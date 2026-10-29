"""Own one warm app-server while keeping every tool process request-bound."""
import json
import copy
import sys
import threading
import time
from contextlib import contextmanager

from openai_codex.errors import InvalidRequestError
from openai_codex.generated.v2_all import ThreadLoadedListResponse
from pydantic import BaseModel, ConfigDict, StrictBool, Field

from .worker_transport import WorkspaceStorageError
from .run_details import cleanup_failure, read_thread_metadata


class RuntimeState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int
    retention_seconds: int
    expires_at: float
    sweep_seconds: int
    max_bytes: int
    cleaning: StrictBool
    usage_bytes: int
    admission_blocked: StrictBool
    runtime_active: StrictBool
    tools: dict | None = Field(default=None, exclude=True, repr=False)
    phase_timings_ms: dict = Field(default_factory=dict, exclude=True, repr=False)


class RuntimePaused(BaseModel):
    paused: StrictBool
    tools: dict = Field(default_factory=dict)


class RuntimeBound(BaseModel):
    bound: StrictBool


class RuntimeClosed(BaseModel):
    closed: StrictBool


class RuntimeSessions:
    def __init__(self, factory, profile_url="http://mcp:8080", agent_ip="10.203.17.30", cleanup_status=None):
        self.factory, self.profile_url, self.agent_ip = factory, profile_url, agent_ip
        self.cleanup_status = cleanup_status
        self.client, self.identity = None, None
        self.active, self.failed = False, False
        self.guard = threading.RLock()
        self.thread, self.thread_options, self.instructions = None, None, None

    @staticmethod
    def _rpc(client, operation, params, response):
        # ponytail: SDK 0.163 has no public transport extension; use its typed request wrapper.
        return client._client.request("taskopotamus/runtime/" + operation, params, response_model=response)

    def _close(self):
        client, self.client, self.identity = self.client, None, None
        self.thread, self.thread_options, self.instructions = None, None, None
        if client is not None:
            try:
                acknowledgment = self._rpc(client, "close", {}, RuntimeClosed)
                if acknowledgment.closed is not True:
                    raise RuntimeError("Runtime cleanup was not confirmed.")
            except Exception:
                confirmed = False
                if self.cleanup_status is not None:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            confirmed = self.cleanup_status().get("runtime_active") is False
                        except Exception:
                            break
                        if confirmed:
                            break
                        time.sleep(.05)
                # Never lend another request a worker with an uncertain cleanup outcome.
                if not confirmed:
                    self.failed = True
                    raise RuntimeError("Runtime cleanup was not confirmed; restart the agent service.") from None
            finally:
                process = client._client._proc
                try:
                    client.close()
                except (BrokenPipeError, OSError):
                    # SDK 0.163 can stop before reaping a transport with a broken stdin.
                    pass
                finally:
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=3)
                        for name in ("_reader_thread", "_stderr_thread"):
                            thread = getattr(client._client, name, None)
                            if thread is not None:
                                thread.join(timeout=.5)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                try:
                                    stream.close()
                                except OSError:
                                    pass

    def close(self):
        with self.guard:
            if self.active:
                raise BlockingIOError("Agent is busy.")
            self._close()

    def shutdown(self):
        with self.guard:
            self.failed = True
            self._close()

    @contextmanager
    def authentication(self):
        with self.guard:
            if self.active:
                raise BlockingIOError("Agent is busy.")
            self._close()
            yield

    def _alive(self):
        process = self.client._client._proc
        return process is not None and process.poll() is None

    @contextmanager
    def lease(self, chat_id, configuration, token, artifact_token, timeout):
        deadline = time.monotonic() + timeout
        identity = (chat_id, json.dumps(configuration, sort_keys=True))
        with self.guard:
            if self.failed:
                raise RuntimeError("Runtime cleanup requires an agent service restart.")
            if self.active:
                raise BlockingIOError("Agent is busy.")
            self.active = True
            try:
                if self.client is not None and (not self._alive() or self.identity != identity):
                    self._close()
                reused = self.client is not None
                if self.client is None:
                    self.client = self.factory()
                    self.identity = identity
                client = self.client
            except BaseException:
                self.active = False
                raise
        finished, lease = False, None
        try:
            try:
                ready = self._rpc(client, "begin", {"scope": {
                    "profile_token": token, "artifact_token": artifact_token, "profile_url": self.profile_url,
                    "agent_ip": self.agent_ip, "deadline": time.time() + deadline - time.monotonic(),
                }}, RuntimeState)
                workspace = ready.model_dump()
            except Exception:
                with self.guard:
                    self._close()
                    blocked = False
                    if self.cleanup_status is not None:
                        try:
                            blocked = self.cleanup_status().get("admission_blocked") is True
                        except Exception:
                            pass
                    if blocked:
                        raise WorkspaceStorageError("Workspace storage limit reached.") from None
                raise
            lease = RuntimeLease(client, workspace, reused, deadline=deadline, sessions=self, tools=ready.tools,
                                 begin_timings=ready.phase_timings_ms)
            yield lease
            if not lease.discarded:
                lease.finish()
                finished = True
        finally:
            with self.guard:
                try:
                    if not finished:
                        try:
                            self._close()
                        except Exception:
                            if lease is None or not lease.published:
                                raise
                            print("Completed reply retained; runtime cleanup requires recovery.", file=sys.stderr, flush=True)
                finally:
                    self.active = False


class RuntimeLease:
    def __init__(self, client, workspace, reused, deadline=None, sessions=None, tools=None, begin_timings=None):
        self.client, self.workspace, self.reused = client, workspace, reused
        self.finished = False
        self.published, self.discarded = False, False
        self.parent_thread_id = None
        self.native_threads = {}
        self.metadata_incomplete = False
        self.cleanup_error = None
        self.cleanup_counts = {}
        self.deadline = deadline
        self.sessions, self.tools = sessions, tools
        self.bound = False
        self.thread_action = "start"
        self.tool_counts = {}
        # Privileged diagnostics are private and may only contribute these numeric phases.
        self.begin_timings = {
            "runtime_" + name: value for name, value in (begin_timings or {}).items()
            if name in ("boundary", "prepare", "stage_inputs", "write_scope", "helper_transition")
            and type(value) is int and 0 <= value <= 1860000
        }

    def resume(self, thread_id, **options):
        cached = self.sessions.thread if self.sessions is not None else None
        if self.tools is not None and cached is not None and cached.id == thread_id:
            previous = copy.deepcopy(self.sessions.thread_options)
            current = copy.deepcopy(options)
            previous.pop("developer_instructions", None)
            current.pop("developer_instructions", None)
            if previous == current:
                self.thread_action = "reuse"
                return cached
            self.thread_action = "configure"
        else:
            self.thread_action = "resume"
        try:
            return self.client.thread_resume(thread_id, **options)
        except InvalidRequestError as error:
            # Only pre-turn archived recovery is retried; model turns and their effects are never replayed.
            if " is archived." not in error.message:
                raise
            self.client.thread_unarchive(thread_id)
            return self.client.thread_resume(thread_id, **options)

    def instructions_current(self, instructions):
        return self.tools is not None and self.sessions is not None and self.sessions.instructions == instructions

    def bind(self, thread, options, instructions):
        self.parent_thread_id = thread.id
        if self.tools is None:
            return
        result = RuntimeSessions._rpc(self.client, "bind", {"thread": thread.id}, RuntimeBound)
        if result.bound is not True:
            raise RuntimeError("Conversation binding was not confirmed.")
        self.bound = True
        if self.sessions is not None:
            self.sessions.thread, self.sessions.thread_options = thread, copy.deepcopy(options)
            self.sessions.instructions = instructions

    def _loaded(self):
        result, cursor, visited = [], None, set()
        while True:
            response = self.client._client.request("thread/loaded/list", {"cursor": cursor, "limit": 100},
                                                   response_model=ThreadLoadedListResponse)
            result.extend(response.data)
            if len(result) > 1024:
                raise RuntimeError("Unexpected loaded thread count.")
            cursor = response.next_cursor
            if cursor is None:
                return result
            if cursor in visited:
                raise RuntimeError("Invalid loaded thread pagination.")
            visited.add(cursor)

    def finish(self):
        if self.finished:
            return
        stage, target = "list_loaded", None
        try:
            loaded = self._loaded()
            metadata_deadline = min(self.deadline or float("inf"), time.monotonic() + 2)
            for thread_id in loaded[:21]:
                if time.monotonic() >= metadata_deadline:
                    self.metadata_incomplete = True
                    break
                try:
                    metadata = read_thread_metadata(self.client._client, thread_id, metadata_deadline - time.monotonic())
                    if metadata is not None and metadata["id"] == thread_id:
                        metadata["role"] = (
                            "parent" if thread_id == self.parent_thread_id
                            else "child" if self.parent_thread_id and metadata["parent_id"] == self.parent_thread_id
                            else "unlinked" if self.parent_thread_id else "unknown"
                        )
                        self.native_threads[thread_id] = metadata
                    else:
                        self.metadata_incomplete = True
                except Exception:
                    # Optional settings evidence must not change the cleanup or paid turn outcome.
                    self.metadata_incomplete = True
            self.metadata_incomplete |= len(self.native_threads) != len(loaded)
            self.cleanup_counts = {
                "loaded_threads": len(loaded),
                "ephemeral_threads": sum(value["ephemeral"] for value in self.native_threads.values()),
                "persistent_threads": sum(not value["ephemeral"] for value in self.native_threads.values()),
                "metadata_unavailable": len(loaded) - len(self.native_threads),
            }
            if self.bound:
                stage = "verify_unloaded"
                if self.parent_thread_id not in loaded or self.metadata_incomplete:
                    raise RuntimeError("Retained conversation state is incomplete.")
                for identifier, metadata in self.native_threads.items():
                    if (metadata["status"] != "idle" or
                            (identifier != self.parent_thread_id and metadata["parent_id"] != self.parent_thread_id)):
                        raise RuntimeError("Retained conversation work is not quiescent.")
                stage = "pause"
                response = RuntimeSessions._rpc(self.client, "end", {"quiescent": True}, RuntimePaused)
                if response.paused is not True:
                    raise RuntimeError("Runtime did not enter idle state.")
                self.tool_counts = {key: value for key, value in response.tools.items()
                                    if key in ("calls", "child_calls", "rejected", "catalog_lists") and type(value) is int and value >= 0}
                self.cleanup_counts["retained_threads"] = len(loaded)
                self.finished = True
                return
            archived_parent = False
            stage = "verify_unloaded"
            if loaded and not self.parent_thread_id:
                raise RuntimeError("Loaded threads have no owning conversation.")
            if self.parent_thread_id in loaded:
                stage, target = "archive", self.native_threads.get(self.parent_thread_id, {"role": "parent"})
                self.client.thread_archive(self.parent_thread_id)
                archived_parent = True
            stage, target = "verify_unloaded", None
            remaining = self._loaded() if loaded else []
            self.cleanup_counts["remaining_after_parent"] = len(remaining)
            if remaining:
                raise RuntimeError("Native threads remained loaded after conversation cleanup.")
            if archived_parent:
                stage, target = "unarchive", self.native_threads.get(self.parent_thread_id, {"role": "parent"})
                # Only restore the conversation; child rollout state is not needed for the next request.
                self.client.thread_unarchive(self.parent_thread_id)
            stage, target = "verify_unloaded", None
            if self._loaded():
                raise RuntimeError("Native threads did not unload.")
            stage = "pause"
            response = RuntimeSessions._rpc(self.client, "end", {"unloaded": True}, RuntimePaused)
            if response.paused is not True:
                raise RuntimeError("Runtime did not enter idle state.")
            self.finished = True
        except Exception as error:
            self.cleanup_error = cleanup_failure(stage, error, target)
            raise
