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
from .run_details import summarize_run
from .compaction_policy import CompactionPolicy
from .instruction_refresh import refresh_instructions
from .subagents import SubagentPolicy
from src.messaging.delivery_store import DeliveryStore
from .attachment_tools import attachment_config
from .google_tools import google_config


FAILURE_TEXT = "This request could not finish or was interrupted. Its effects may be incomplete; check before trying again."


class ChatService:
    def __init__(self, codex_factory, database, model="gpt-6-astra", effort="medium", turn_timeout=180, auto_compact_percent=None, profiles=None, subagents=None, workspace_status=None, artifacts=None, deliveries=None):
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
                profile, profile_config, token = self.profiles.prepare(chat_id)
                # Saved user text must never acquire developer authority.
                turn_input = json.dumps({"profile": profile, "message": text}, ensure_ascii=False)
            if self.artifacts is not None:
                accepted = self.artifacts.accept_inputs(chat_id, request_id, attachments, self.turn_timeout + 60)
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
                capability_deadline = self.artifacts.clock() + self._remaining(deadline)
                capability = self.artifacts.begin_run(chat_id, request_id, self._remaining(deadline), deadline=capability_deadline)
                runtime.callback(self._revoke_artifacts, chat_id, request_id)
                if cancelled.is_set():
                    raise TimeoutError("Request cancelled before worker access.")
                profile_config.update(attachment_config())
                profile_config.update(google_config())
                client = self.codex_factory(token, capability)
            elif self.profiles is not None:
                client = self.codex_factory(token)
            else:
                client = self.codex_factory()
            # Start after the replay check; loaded threads can ignore resume configuration changes.
            codex = runtime.enter_context(client)
            workspace, expired = None, False
            if self.workspace_status is not None:
                workspace = self.workspace_status()
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
            if codex.account().account is None:
                raise PermissionError("Sign in to Codex first.")
            db.execute("INSERT INTO replies VALUES (%s, %s, %s, NULL)", (chat_id, request_id, digest))
            if include_details:
                db.execute("INSERT INTO reply_runs VALUES (%s, %s, 'pending', NULL)", (chat_id, request_id))
            db.commit()
            try:
                row = db.execute("SELECT thread_id FROM conversations WHERE chat_id = %s", (chat_id,)).fetchone()
                options = {
                    "model": self.model, "sandbox": Sandbox.full_access, "approval_mode": ApprovalMode.deny_all,
                    "cwd": "/workspace",
                    "config": {
                        "model_reasoning_effort": self.effort, "features.shell_tool": True,
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
                if row:
                    thread = codex.thread_resume(row[0], **options)
                    if self.profiles is not None or workspace is not None or self.artifacts is not None:
                        refresh_instructions(thread, instructions)
                else:
                    thread = codex.thread_start(**options)
                    db.execute("INSERT INTO conversations VALUES (%s, %s)", (chat_id, thread.id))
                    db.commit()
                # Failed turns and model switches must not leave stale window metadata behind.
                db.execute("DELETE FROM thread_context WHERE thread_id = %s", (thread.id,))
                db.commit()
                turn = thread.turn(turn_input)
                active_turn.append(turn)
                if cancelled.is_set():
                    self._interrupt(turn)
                    raise TimeoutError("Request cancelled.")
                response = turn.run()
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
                    publication["result"] = {**result, **({"status": "completed", "details": None} if include_details else {})}
            except Exception:
                db.rollback()
                try:
                    self._fail_request(chat_id, request_id, digest)
                except Exception:
                    print("Failure finalization unavailable; request outcome uncertain.", file=sys.stderr, flush=True)
                raise
            if include_details:
                details = None
                try:
                    details = summarize_run(response, self.model, self.effort, self.subagents)
                except Exception:
                    # A telemetry defect must not discard a completed reply or cause another paid turn.
                    pass
                self._save_run(db, chat_id, request_id, "completed", details)
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
