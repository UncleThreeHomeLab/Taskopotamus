import hashlib
import json
import re
import sys
import threading
import time
import traceback
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import ExitStack

from openai_codex import ApprovalMode, Sandbox, TextInput, LocalImageInput
from openai_codex.errors import JsonRpcError

from src.profiles.instructions import load_system_instructions
from src.profiles.auth import ProfileTokens
from .run_details import cleanup_failure, summarize_run
from .compaction_policy import CompactionPolicy
from .instruction_refresh import refresh_instructions
from .subagents import SubagentPolicy
from src.messaging.delivery_store import DeliveryStore
from .attachment_tools import attachment_config
from .google_tools import google_config
from .request_tools import request_config


FAILURE_TEXT = "This request could not finish or was interrupted. Its effects may be incomplete; check before trying again."


class ChatService:
    def __init__(self, codex_factory, database, model="gpt-6.1-sol", effort="medium", turn_timeout=180, auto_compact_percent=None, profiles=None, subagents=None, workspace_status=None, artifacts=None, deliveries=None, runtime_sessions=None):
        self.codex_factory = codex_factory
        self.database = database
        self.model = model
        self.effort = effort
        self.turn_timeout = turn_timeout
        self.compaction = CompactionPolicy(auto_compact_percent)
        self.profiles = profiles
        self.subagents = subagents or SubagentPolicy()
        self.workspace_status = workspace_status
        self.artifacts = artifacts
        self.deliveries = deliveries or DeliveryStore(database)
        self.runtime_sessions = runtime_sessions
        self.lock = threading.Lock()
        with database.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations (chat_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS thread_context (
                thread_id TEXT PRIMARY KEY, model TEXT NOT NULL, context_window INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS replies (
                chat_id TEXT, request_id TEXT, input_hash TEXT NOT NULL, reply TEXT,
                PRIMARY KEY (chat_id, request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS reply_runs (
                chat_id TEXT, request_id TEXT, status TEXT NOT NULL, details TEXT,
                PRIMARY KEY (chat_id, request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_workspace (
                chat_id TEXT PRIMARY KEY, generation BIGINT NOT NULL)""")

    @staticmethod
    def validate_chat_id(chat_id):
        ProfileTokens.validate_chat(chat_id)

    def reply(self, chat_id, text, request_id, include_details=False, attachments=None):
        self.validate_chat_id(chat_id)
        attachments = [] if attachments is None else attachments
        if not isinstance(text, str) or len(text) > 16000 or not text.strip() and not attachments:
            raise ValueError("Message must contain between 1 and 16000 characters.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9]{1,20}", request_id):
            raise ValueError("A numeric request ID is required.")
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError("Agent is busy.")
        deadline = time.monotonic() + self.turn_timeout
        try:
            digest = self._fingerprint(chat_id, text, request_id, attachments)
        except Exception:
            self.lock.release()
            raise
        result = Future()
        cancelled = threading.Event()
        active_turn = []
        publication = {"lock": threading.Lock(), "result": None}

        def run():
            try:
                value = self._reply(chat_id, text, request_id, cancelled, active_turn, include_details, publication, attachments,
                                    digest, deadline)
            except Exception as error:
                if isinstance(error, JsonRpcError):
                    # Emit only fixed categories and code locations; RPC messages can contain credentials.
                    hints = [hint for hint in (
                        "mcp", "config", "environment variable", "not found", "no such file",
                        "permission denied", "read-only file system", "rollout", "initialize",
                        "required", "invalid", "expected", "authentication", "timeout",
                    ) if hint in error.message.lower()]
                    frames = traceback.extract_tb(error.__traceback__)
                    location = next((str(frame.lineno) for frame in reversed(frames)
                                     if frame.filename.endswith("chat_service.py")), "unknown")
                    print(f"Codex RPC failed (code={error.code}; chat_service.py:{location}; categories={','.join(hints) or 'unclassified'}).",
                          file=sys.stderr, flush=True)
                self.lock.release()
                result.set_exception(error)
            else:
                self.lock.release()
                result.set_result(value)

        threading.Thread(target=run, daemon=True).start()
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            with publication["lock"]:
                if publication["result"] is not None:
                    return dict(publication["result"])
                cancelled.set()
            if active_turn:
                threading.Thread(target=self._interrupt, args=(active_turn[0],), daemon=True).start()
            try:
                self._fail_request(chat_id, request_id, digest)
            except Exception:
                # Fixed capability deadlines still fail closed if the database cannot record cancellation.
                print("Timeout finalization unavailable; request outcome uncertain.", file=sys.stderr, flush=True)
            # A stuck worker keeps the slot: never accumulate more paid turns behind it.
            raise TimeoutError("Codex reply timed out.") from None

    @staticmethod
    def _interrupt(turn):
        try:
            turn.interrupt()
        except Exception:
            pass

    def _revoke_artifacts(self, chat_id, request_id):
        try:
            self.artifacts.cancel_run(chat_id, request_id)
        except Exception:
            print("Artifact scope cleanup unavailable.", file=sys.stderr, flush=True)

    def _fingerprint(self, chat_id, text, request_id, attachments):
        inputs = []
        if attachments:
            if self.artifacts is None:
                raise ValueError("File inputs are unavailable.")
            inputs = self.artifacts.input_identity(chat_id, request_id, attachments)
        return hashlib.sha256((text if not attachments else json.dumps([text, inputs], sort_keys=True, ensure_ascii=False)).encode()).hexdigest()

    @staticmethod
    def _visible_text(manifest):
        return "\n\n".join(item["text"] for item in manifest["items"] if item["type"] == "text")

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request deadline expired.")
        return remaining

    def _fail_request(self, chat_id, request_id, digest):
        # Caller timeout never holds the publication lock while this transaction waits for database locks.
        with self.database.connect() as db:
            if self.artifacts is not None:
                db.execute("SELECT state FROM artifact_runs WHERE chat_id=%s AND request_id=%s FOR UPDATE", (chat_id, request_id))
            self.deliveries._lock(db, chat_id, request_id)
            row = db.execute("SELECT input_hash FROM replies WHERE chat_id=%s AND request_id=%s FOR UPDATE", (chat_id, request_id)).fetchone()
            if row and row[0] != digest:
                raise ValueError("Request ID already used for another message.")
            manifest = self.deliveries.get(chat_id, request_id, db)
            if manifest is None:
                if self.artifacts is not None:
                    self.artifacts.end_run(db, chat_id, request_id, "failed")
                self.deliveries.publish(db, chat_id, request_id, [{"type": "text", "text": FAILURE_TEXT}], outcome="failed")
                manifest = self.deliveries.get(chat_id, request_id, db)
            visible = self._visible_text(manifest)
            db.execute("""INSERT INTO replies VALUES (%s,%s,%s,%s) ON CONFLICT(chat_id,request_id)
                DO UPDATE SET reply=EXCLUDED.reply""", (chat_id, request_id, digest, visible))
            db.execute("""INSERT INTO reply_runs VALUES (%s,%s,%s,NULL) ON CONFLICT(chat_id,request_id)
                DO UPDATE SET status=EXCLUDED.status WHERE reply_runs.status='pending'""",
                (chat_id, request_id, manifest["reply_status"]))
            return {"text": visible, **manifest}

    def _reply(self, chat_id, text, request_id, cancelled, active_turn, include_details=False, publication=None, attachments=None,
               digest=None, deadline=None):
        attachments = [] if attachments is None else attachments
        digest = digest or self._fingerprint(chat_id, text, request_id, attachments)
        deadline = deadline if deadline is not None else time.monotonic() + self.turn_timeout
        started = time.monotonic()
        timings, lease = {}, None
        with ExitStack() as runtime, self.database.connect() as db:
            row = db.execute(
                "SELECT input_hash, reply FROM replies WHERE chat_id = %s AND request_id = %s", (chat_id, request_id),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("Request ID already used for another message.")
                # A crash may leave uncertain work. Fail closed instead of charging for it again.
                manifest = self.deliveries.get(chat_id, request_id, db)
                if row[1] is None and manifest is None:
                    db.rollback()
                    self._fail_request(chat_id, request_id, digest)
                    manifest = self.deliveries.get(chat_id, request_id, db)
                result = {"text": self._visible_text(manifest) if manifest else row[1] or FAILURE_TEXT}
                result.update(manifest or {"items": [{"type": "text", "text": result["text"]}]})
                self._artifact_metadata(result, chat_id, db)
                if include_details:
                    saved = db.execute(
                        "SELECT status, details FROM reply_runs WHERE chat_id = %s AND request_id = %s",
                        (chat_id, request_id),
                    ).fetchone()
                    result.update(
                        status=manifest["reply_status"]
                        if manifest
                        else saved[0]
                        if saved and saved[0] != "pending"
                        else "uncertain",
                        details=json.loads(saved[1]) if saved and saved[1] else None,
                    )
                return result
            if cancelled.is_set():
                raise TimeoutError("Request cancelled.")
            instructions, profile_config = load_system_instructions(), {}
            turn_input, token, capability = text, None, None
            if self.profiles is not None:
                profile_started = time.monotonic()
                profile, profile_config, token = self.profiles.prepare(chat_id)
                timings["profile_prepare"] = round((time.monotonic() - profile_started) * 1000)
                # Saved user text must never acquire developer authority.
                turn_input = json.dumps({"profile": profile, "message": text}, ensure_ascii=False)
            if self.artifacts is not None:
                inputs_started = time.monotonic()
                accepted = self.artifacts.accept_inputs(chat_id, request_id, attachments, self.turn_timeout + 60, db=db)
                timings["artifact_inputs"] = round((time.monotonic() - inputs_started) * 1000)
                if attachments:
                    from src.worker.input_files import input_path

                    information = [
                        {**item, "path": input_path(accepted["request_tag"], item)} for item in accepted["files"]
                    ]
                    content = {"message": text, "attachments": information}
                    if self.profiles is not None:
                        content["profile"] = profile
                    turn_input = [TextInput(json.dumps(content, ensure_ascii=False))]
                    turn_input.extend(
                        LocalImageInput(item["path"]) for item in information if item.get("presentation_mime")
                    )
                scope_started = time.monotonic()
                capability_deadline = self.artifacts.clock() + self._remaining(deadline)
                capability = self.artifacts.begin_run(chat_id, request_id, self._remaining(deadline), deadline=capability_deadline, db=db)
                runtime.callback(self._revoke_artifacts, chat_id, request_id)
                try:
                    db.commit()  # Publish admission and release capacity lock before worker access.
                except Exception:
                    # A failed commit may have succeeded; cleanup still revokes on a fresh connection.
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    raise
                timings["artifact_scope"] = round((time.monotonic() - scope_started) * 1000)
                if cancelled.is_set():
                    raise TimeoutError("Request cancelled before worker access.")
                if self.runtime_sessions is None:
                    profile_config.update(attachment_config())
                    profile_config.update(google_config())
            if self.runtime_sessions is not None:
                profile_config = request_config()
            timings["preparation"] = round((time.monotonic() - started) * 1000)
            acquisition = time.monotonic()
            if self.runtime_sessions is not None:
                configuration = {"model": self.model, "effort": self.effort, "tools": profile_config,
                                 "subagents": self.subagents.config(), "compact_percent": self.compaction.percent}
                lease = runtime.enter_context(self.runtime_sessions.lease(
                    chat_id, configuration, token, capability, self._remaining(deadline)))
                codex = lease.client
                timings.update(lease.begin_timings)
                if lease.tools is not None:
                    profile_config = request_config(lease.tools)
            else:
                if self.artifacts is not None:
                    client = self.codex_factory(token, capability)
                elif self.profiles is not None:
                    client = self.codex_factory(token)
                else:
                    client = self.codex_factory()
                codex = runtime.enter_context(client)
            timings["runtime_begin"] = round((time.monotonic() - acquisition) * 1000)
            workspace, expired = lease.workspace if lease else None, False
            if lease is not None or self.workspace_status is not None:
                workspace = workspace if lease else self.workspace_status()
                previous = db.execute(
                    "SELECT generation FROM conversation_workspace WHERE chat_id = %s", (chat_id,)
                ).fetchone()
                expired = previous is not None and previous[0] != workspace["generation"]
                instructions += (
                    "\n\nCurrent trusted workspace state: "
                    + json.dumps(workspace)
                    + ". This replaces earlier workspace state. Files from earlier generations have expired. "
                    "Check file existence before reusing work. Conversation history is preserved."
                )
            account_started = time.monotonic()
            if codex.account().account is None:
                raise PermissionError("Sign in to Codex first.")
            timings["account"] = round((time.monotonic() - account_started) * 1000)
            thread_started = time.monotonic()
            db.execute("INSERT INTO replies VALUES (%s, %s, %s, NULL)", (chat_id, request_id, digest))
            db.execute("INSERT INTO reply_runs VALUES (%s, %s, 'pending', NULL)", (chat_id, request_id))
            db.commit()
            try:
                row = db.execute("SELECT thread_id FROM conversations WHERE chat_id = %s", (chat_id,)).fetchone()
                options = {
                    "model": self.model, "sandbox": Sandbox.full_access, "approval_mode": ApprovalMode.deny_all,
                    "cwd": "/workspace",
                    "config": {
                        "model_reasoning_effort": self.effort, "features.shell_tool": True,
                        "service_tier": "fast", "features.fast_mode": True,
                        "features.apps": False, "web_search": "disabled", **self.subagents.config(),
                        **profile_config,
                    },
                    "developer_instructions": instructions,
                }
                if row:
                    context = db.execute(
                        "SELECT context_window FROM thread_context WHERE thread_id = %s AND model = %s",
                        (row[0], self.model),
                    ).fetchone()
                    options["config"].update(self.compaction.config(context[0] if context else None))
                if cancelled.is_set():
                    raise TimeoutError("Request cancelled.")
                acquiring_thread = time.monotonic()
                if row:
                    thread = lease.resume(row[0], **options) if lease else codex.thread_resume(row[0], **options)
                    timings["thread_acquire"] = round((time.monotonic() - acquiring_thread) * 1000)
                    refreshing = time.monotonic()
                    if ((self.profiles is not None or workspace is not None or self.artifacts is not None)
                            and not (lease and lease.instructions_current(instructions))):
                        refresh_instructions(thread, instructions)
                    timings["instruction_refresh"] = round((time.monotonic() - refreshing) * 1000)
                else:
                    thread = codex.thread_start(**options)
                    timings["thread_acquire"] = round((time.monotonic() - acquiring_thread) * 1000)
                    timings["instruction_refresh"] = 0
                    db.execute("INSERT INTO conversations VALUES (%s, %s)", (chat_id, thread.id))
                    db.commit()
                if lease is not None:
                    binding = time.monotonic()
                    lease.bind(thread, options, instructions)
                    timings["request_binding"] = round((time.monotonic() - binding) * 1000)
                # Failed turns and model switches must not leave stale window metadata behind.
                db.execute("DELETE FROM thread_context WHERE thread_id = %s", (thread.id,))
                db.commit()
                preparing_turn = time.monotonic()
                turn = thread.turn(turn_input)
                timings["turn_prepare"] = round((time.monotonic() - preparing_turn) * 1000)
                active_turn.append(turn)
                if cancelled.is_set():
                    self._interrupt(turn)
                    raise TimeoutError("Request cancelled.")
                timings["thread_setup"] = round((time.monotonic() - thread_started) * 1000)
                timings["setup_total"] = round((time.monotonic() - started) * 1000)
                native_started = time.monotonic()
                response = turn.run()
                timings["native_turn"] = round((time.monotonic() - native_started) * 1000)
                if cancelled.is_set():
                    raise TimeoutError("Request cancelled before publication.")
                # Learn from runtime usage, never from a model-name table or private catalog cache.
                window = getattr(getattr(response, "usage", None), "model_context_window", None)
                if type(window) is int and window > 0:
                    db.execute("INSERT INTO thread_context VALUES (%s, %s, %s)", (thread.id, self.model, window))
                if response.status.value != "completed":
                    raise RuntimeError("Codex did not complete its reply.")
                scope_deadline = None
                if self.artifacts is not None:
                    scope_deadline = self.artifacts.active_run(db, chat_id, request_id)
                self.deliveries._lock(db, chat_id, request_id)
                self._remaining(deadline)
                if self.artifacts is not None:
                    self.artifacts.active_run(db, chat_id, request_id)
                queued = self.deliveries.snapshot(chat_id, request_id, db)
                if queued is not None:
                    if queued["reply_status"] != "pending":
                        raise TimeoutError("Request already finalized.")
                    self.deliveries.finish(db, chat_id, request_id)
                    if self.artifacts is not None:
                        self.artifacts.end_run(db, chat_id, request_id, "completed")
                else:
                    reply = response.final_response
                    if not isinstance(reply, str) or not reply.strip():
                        reply = "The agent finished without preparing a response. Please try again."
                    if expired:
                        reply = "Temporary workspace files expired and were cleaned. Saved files and conversation history remain.\n\n" + reply
                    items = [{"type": "text", "text": reply}]
                    if self.artifacts is not None:
                        items.extend(self.artifacts.publish(db, chat_id, request_id))
                    self.deliveries.publish(db, chat_id, request_id, items)
                manifest = self.deliveries.get(chat_id, request_id, db)
                reply = self._visible_text(manifest)
                db.execute("UPDATE replies SET reply = %s WHERE chat_id = %s AND request_id = %s", (reply, chat_id, request_id))
                if workspace is not None:
                    db.execute("""INSERT INTO conversation_workspace VALUES (%s, %s)
                        ON CONFLICT (chat_id) DO UPDATE SET generation = EXCLUDED.generation""", (chat_id, workspace["generation"]))
                result = {"text": reply, **manifest}
                self._artifact_metadata(result, chat_id, db)
                with publication["lock"]:
                    if cancelled.is_set():
                        raise TimeoutError("Request cancelled before publication.")
                    self._remaining(deadline)
                    if scope_deadline is not None and scope_deadline <= self.artifacts.clock():
                        raise TimeoutError("Reply capability expired before publication.")
                    db.commit()
                    if lease is not None:
                        lease.published = True
                    publication["result"] = {**result, **({"status": "completed", "details": None} if include_details else {})}
            except Exception:
                db.rollback()
                try:
                    self._fail_request(chat_id, request_id, digest)
                except Exception:
                    print("Failure finalization unavailable; request outcome uncertain.", file=sys.stderr, flush=True)
                raise
            cleanup_started = time.monotonic()
            cleanup_error = None
            if lease is not None:
                try:
                    lease.finish()
                except Exception:
                    lease.discarded = True
                    cleanup_error = lease.cleanup_error
                    print("Completed reply retained; warm runtime discarded after cleanup failure.", file=sys.stderr, flush=True)
            try:
                # Close now, while the database is usable, so disposal is timed and callbacks run once.
                runtime.close()
            except Exception as error:
                cleanup_error = cleanup_failure("runtime_exit", error)
                print("Completed reply retained; runtime cleanup requires recovery.", file=sys.stderr, flush=True)
            timings["cleanup"] = round((time.monotonic() - cleanup_started) * 1000)
            timings["request_total"] = round((time.monotonic() - started) * 1000)
            details = None
            try:
                details = summarize_run(
                    response, self.model, self.effort, self.subagents, parent_id=thread.id,
                    native_threads=lease.native_threads if lease else None,
                    metadata_incomplete=lease.metadata_incomplete if lease else False,
                )
                details.update(system_timings_ms=timings, runtime_reused=lease.reused if lease else False,
                               runtime_retained=bool(lease and lease.finished and not lease.discarded),
                               thread_action=lease.thread_action if lease else "unavailable",
                               tools_retained=bool(lease and lease.bound and lease.finished and not lease.discarded),
                               tool_activity=lease.tool_counts if lease else {},
                               cleanup=lease.cleanup_counts if lease else {}, cleanup_error=cleanup_error)
            except Exception:
                # A telemetry defect must not discard a completed reply or cause another paid turn.
                pass
            self._save_run(db, chat_id, request_id, "completed", details)
            if include_details:
                result.update(status="completed", details=details)
            return result

    def _artifact_metadata(self, result, chat_id, db):
        if self.artifacts is None:
            return
        information = {}
        for item in result.get("items", []):
            if item["type"] in ("image", "file"):
                try:
                    information[item["artifact_id"]] = self.artifacts.describe(item["artifact_id"], chat_id, db=db, committed=True)
                except PermissionError:
                    information[item["artifact_id"]] = {"artifact_id": item["artifact_id"], "available": False}
        if information:
            result["artifacts"] = information

    @staticmethod
    def _save_run(db, chat_id, request_id, status, details):
        try:
            with db.transaction():
                db.execute("UPDATE reply_runs SET status = %s, details = %s WHERE chat_id = %s AND request_id = %s",
                           (status, json.dumps(details) if details is not None else None, chat_id, request_id))
        except Exception as error:
            print(f"Run details unavailable ({type(error).__name__}).", file=sys.stderr, flush=True)

    def reset(self, chat_id):
        self.validate_chat_id(chat_id)
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError("Agent is busy.")
        try:
            if self.runtime_sessions is not None:
                self.runtime_sessions.close()
            with self.database.connect() as db:
                db.execute(
                    "DELETE FROM thread_context WHERE thread_id IN (SELECT thread_id FROM conversations WHERE chat_id = %s)",
                    (chat_id,),
                )
                db.execute("DELETE FROM conversations WHERE chat_id = %s", (chat_id,))
                db.execute("DELETE FROM conversation_workspace WHERE chat_id = %s", (chat_id,))
        finally:
            self.lock.release()
        return {"text": "New conversation started. What would you like to talk about?"}
