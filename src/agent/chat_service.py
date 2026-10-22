import hashlib
import re
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import ExitStack

from openai_codex import ApprovalMode, Sandbox

from src.runtime import private_database
from .compaction_policy import CompactionPolicy


class ChatService:
    def __init__(self, codex_factory, database, model="gpt-6-astra", effort="medium", turn_timeout=180, auto_compact_percent=None):
        self.codex_factory = codex_factory
        self.database = database
        self.model = model
        self.effort = effort
        self.turn_timeout = turn_timeout
        self.compaction = CompactionPolicy(auto_compact_percent)
        self.lock = threading.Lock()
        with private_database(database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations (chat_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS thread_context (
                thread_id TEXT PRIMARY KEY, model TEXT NOT NULL, context_window INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS replies (
                chat_id TEXT, request_id TEXT, input_hash TEXT NOT NULL, reply TEXT,
                PRIMARY KEY (chat_id, request_id))""")

    @staticmethod
    def validate_chat_id(chat_id):
        if not isinstance(chat_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", chat_id):
            raise ValueError("A numeric private chat ID is required.")

    def reply(self, chat_id, text, request_id):
        self.validate_chat_id(chat_id)
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError("Message must contain between 1 and 16000 characters.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9]{1,20}", request_id):
            raise ValueError("A numeric request ID is required.")
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError("Agent is busy.")
        result = Future()
        cancelled = threading.Event()
        active_turn = []

        def run():
            try:
                value = self._reply(chat_id, text, request_id, cancelled, active_turn)
            except Exception as error:
                self.lock.release()
                result.set_exception(error)
            else:
                self.lock.release()
                result.set_result(value)

        threading.Thread(target=run, daemon=True).start()
        try:
            return result.result(timeout=self.turn_timeout)
        except FutureTimeout:
            cancelled.set()
            if active_turn:
                threading.Thread(target=self._interrupt, args=(active_turn[0],), daemon=True).start()
            # A stuck worker keeps the slot: never accumulate more paid turns behind it.
            raise TimeoutError("Codex reply timed out.") from None

    @staticmethod
    def _interrupt(turn):
        try:
            turn.interrupt()
        except Exception:
            pass

    def _reply(self, chat_id, text, request_id, cancelled, active_turn):
        digest = hashlib.sha256(text.encode()).hexdigest()
        with ExitStack() as runtime, private_database(self.database) as db:
            row = db.execute(
                "SELECT input_hash, reply FROM replies WHERE chat_id = ? AND request_id = ?", (chat_id, request_id),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("Request ID already used for another message.")
                # A crash may leave uncertain work. Fail closed instead of charging for it again.
                return {"text": row[1] or "Previous request was interrupted. Please send your message again."}
            if cancelled.is_set():
                raise TimeoutError("Request cancelled.")
            # Start after the replay check; loaded threads can ignore resume configuration changes.
            codex = runtime.enter_context(self.codex_factory())
            if codex.account().account is None:
                raise PermissionError("Sign in to Codex first.")
            db.execute("INSERT INTO replies VALUES (?, ?, ?, NULL)", (chat_id, request_id, digest))
            db.commit()
            try:
                row = db.execute("SELECT thread_id FROM conversations WHERE chat_id = ?", (chat_id,)).fetchone()
                options = {
                    "model": self.model, "sandbox": Sandbox.read_only, "approval_mode": ApprovalMode.deny_all,
                    "config": {"model_reasoning_effort": self.effort, "features.shell_tool": False, "web_search": "disabled"},
                    "developer_instructions": (
                        "You are Taskopotamus, a helpful conversational assistant in Telegram. "
                        "Answer naturally and clearly. Keep replies concise unless detail is useful. "
                        "Use the conversation history for follow-up questions. "
                        "This is text chat only: do not run commands, inspect local files, or modify anything."
                    ),
                }
                if row:
                    context = db.execute(
                        "SELECT context_window FROM thread_context WHERE thread_id = ? AND model = ?",
                        (row[0], self.model),
                    ).fetchone()
                    options["config"].update(self.compaction.config(context[0] if context else None))
                if cancelled.is_set():
                    raise TimeoutError("Request cancelled.")
                if row:
                    thread = codex.thread_resume(row[0], **options)
                else:
                    thread = codex.thread_start(**options)
                    db.execute("INSERT INTO conversations VALUES (?, ?)", (chat_id, thread.id))
                    db.commit()
                # Failed turns and model switches must not leave stale window metadata behind.
                db.execute("DELETE FROM thread_context WHERE thread_id = ?", (thread.id,))
                db.commit()
                turn = thread.turn(text)
                active_turn.append(turn)
                if cancelled.is_set():
                    self._interrupt(turn)
                    raise TimeoutError("Request cancelled.")
                response = turn.run()
                # Learn from runtime usage, never from a model-name table or private catalog cache.
                window = getattr(getattr(response, "usage", None), "model_context_window", None)
                if type(window) is int and window > 0:
                    db.execute("INSERT INTO thread_context VALUES (?, ?, ?)", (thread.id, self.model, window))
                if response.status.value != "completed" or not response.final_response:
                    raise RuntimeError("Codex did not complete its reply.")
                reply = response.final_response
            except Exception:
                db.execute(
                    "UPDATE replies SET reply = ? WHERE chat_id = ? AND request_id = ?",
                    ("Codex could not finish this message. Please send your message again.", chat_id, request_id),
                )
                db.commit()
                raise
            db.execute("UPDATE replies SET reply = ? WHERE chat_id = ? AND request_id = ?", (reply, chat_id, request_id))
            return {"text": reply}

    def reset(self, chat_id):
        self.validate_chat_id(chat_id)
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError("Agent is busy.")
        try:
            with private_database(self.database) as db:
                db.execute(
                    "DELETE FROM thread_context WHERE thread_id IN (SELECT thread_id FROM conversations WHERE chat_id = ?)",
                    (chat_id,),
                )
                db.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
        finally:
            self.lock.release()
        return {"text": "New conversation started. What would you like to talk about?"}
