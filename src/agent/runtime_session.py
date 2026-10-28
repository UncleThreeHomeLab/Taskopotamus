"""Own one warm app-server while keeping every tool process request-bound."""
import json
import sys
import threading
import time
from contextlib import contextmanager

from openai_codex.errors import InvalidRequestError
from openai_codex.generated.v2_all import ThreadLoadedListResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from .worker_transport import WorkspaceStorageError


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


class RuntimePaused(BaseModel):
    paused: StrictBool


class RuntimeClosed(BaseModel):
    closed: StrictBool


class RuntimeSessions:
    def __init__(self, factory, profile_url="http://mcp:8080", agent_ip="10.203.17.30", idle_seconds=300, cleanup_status=None):
        if type(idle_seconds) is not int or not 10 <= idle_seconds <= 3600:
            raise ValueError("Runtime idle lifetime must be between 10 and 3600 seconds.")
        self.factory, self.profile_url, self.agent_ip = factory, profile_url, agent_ip
        self.idle_seconds = idle_seconds
        self.cleanup_status = cleanup_status
        self.client, self.identity, self.last_used = None, None, 0
        self.active, self.failed = False, False
        self.guard = threading.RLock()

    @staticmethod
    def _rpc(client, operation, params, response):
        # ponytail: SDK 0.163 has no public transport extension; use its typed request wrapper.
        return client._client.request("taskopotamus/runtime/" + operation, params, response_model=response)

    def _close(self):
        client, self.client, self.identity = self.client, None, None
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
                if self.client is not None and (not self._alive() or self.identity != identity
                                                or time.monotonic() - self.last_used >= self.idle_seconds):
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
                workspace = self._rpc(client, "begin", {"scope": {
                    "profile_token": token, "artifact_token": artifact_token, "profile_url": self.profile_url,
                    "agent_ip": self.agent_ip, "deadline": time.time() + deadline - time.monotonic(),
                }, "idle_seconds": self.idle_seconds}, RuntimeState).model_dump()
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
            lease = RuntimeLease(client, workspace, reused)
            yield lease
            if not lease.discarded:
                lease.finish()
                finished = True
                self.last_used = time.monotonic()
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
    def __init__(self, client, workspace, reused):
        self.client, self.workspace, self.reused = client, workspace, reused
        self.finished = False
        self.published, self.discarded = False, False

    def resume(self, thread_id, **options):
        try:
            return self.client.thread_resume(thread_id, **options)
        except InvalidRequestError as error:
            # Only pre-turn archived recovery is retried; model turns and their effects are never replayed.
            if " is archived." not in error.message:
                raise
            self.client.thread_unarchive(thread_id)
            return self.client.thread_resume(thread_id, **options)

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
        loaded = self._loaded()
        for thread_id in loaded:
            self.client.thread_archive(thread_id)
        for thread_id in loaded:
            # Unarchive restores rollout availability without loading its tools or resuming execution.
            self.client.thread_unarchive(thread_id)
        if self._loaded():
            raise RuntimeError("Native threads did not unload.")
        response = RuntimeSessions._rpc(self.client, "end", {"unloaded": True}, RuntimePaused)
        if response.paused is not True:
            raise RuntimeError("Runtime did not enter idle state.")
        self.finished = True
