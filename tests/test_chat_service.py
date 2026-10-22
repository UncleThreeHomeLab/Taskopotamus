import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.agent.chat_service import ChatService
from src.agent.server import make_handler
from src.telegram.agent_client import AgentClient


class ChatServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "chat.sqlite3"
        self.codex = Mock()
        self.codex.account.return_value = SimpleNamespace(account=object())
        self.thread = Mock(id="synthetic-thread")
        self.turn = self.thread.turn.return_value
        self.turn.run.return_value = SimpleNamespace(status=SimpleNamespace(value="completed"), final_response="Hello")
        self.codex.thread_start.return_value = self.thread
        self.codex.thread_resume.return_value = self.thread
        self.chat = ChatService(self.codex, self.database)

    def test_conversation_survives_service_recreation_and_other_chats_are_separate(self):
        self.assertEqual(self.chat.reply("42", "hello", "1"), {"text": "Hello"})
        recreated = ChatService(self.codex, self.database)
        recreated.reply("42", "follow up", "2")
        self.codex.thread_resume.assert_called_once()
        self.assertEqual(self.codex.thread_resume.call_args.args[0], "synthetic-thread")
        recreated.reply("43", "separate chat", "3")
        self.assertEqual(self.codex.thread_start.call_count, 2)
        options = self.codex.thread_start.call_args.kwargs
        self.assertEqual(options["model"], "gpt-6-astra")
        self.assertEqual(options["config"]["model_reasoning_effort"], "medium")
        self.assertFalse(options["config"]["features.shell_tool"])
        self.assertEqual(options["sandbox"].value, "read-only")

    def test_reset_starts_a_new_thread(self):
        self.chat.reply("42", "hello", "1")
        self.chat.reset("42")
        self.chat.reply("42", "new topic", "2")
        self.assertEqual(self.codex.thread_start.call_count, 2)
        self.codex.thread_resume.assert_not_called()

    def test_validation_and_auth_fail_before_starting_a_turn(self):
        for chat_id, text in (("../bad", "hi"), (42, "hi"), ("42", ""), ("42", "x" * 16001)):
            with self.subTest(chat_id=chat_id), self.assertRaises(ValueError):
                self.chat.reply(chat_id, text, "1")
        self.codex.account.return_value = SimpleNamespace(account=None)
        with self.assertRaises(PermissionError):
            self.chat.reply("42", "hello", "1")
        self.codex.thread_start.assert_not_called()

    def test_failed_turn_does_not_return_partial_output_as_success(self):
        self.turn.run.return_value = SimpleNamespace(status=SimpleNamespace(value="failed"), final_response="partial")
        with self.assertRaises(RuntimeError):
            self.chat.reply("42", "hello", "1")

    def test_chat_http_round_trip_validation_and_safe_error_messages(self):
        token = "synthetic-service-token-for-tests-only"
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Mock(), token, self.chat))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = AgentClient(f"http://127.0.0.1:{server.server_port}", token)
        self.assertEqual(client.chat(42, "hello", "1"), {"text": "Hello"})
        self.assertIn("New conversation", client.reset(42)["text"])
        for payload in ([], {"chat_id": "../bad", "text": "hello"}):
            request = Request(f"{client.base_url}/chat", data=json.dumps(payload).encode(), method="POST", headers={"Authorization": f"Bearer {token}"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        self.codex.account.return_value = SimpleNamespace(account=None)
        with self.assertRaisesRegex(RuntimeError, "signed out"):
            client.chat(42, "hello", "2")
        self.codex.account.side_effect = RuntimeError("synthetic-sensitive-detail")
        with self.assertRaises(RuntimeError) as caught:
            client.chat(42, "hello", "3")
        self.assertNotIn("synthetic-sensitive-detail", str(caught.exception))

    def test_duplicate_request_reuses_saved_reply_after_restart(self):
        self.chat.reply("42", "hello", "1")
        recreated = ChatService(self.codex, self.database)
        self.assertEqual(recreated.reply("42", "hello", "1"), {"text": "Hello"})
        self.turn.run.assert_called_once()
        with self.assertRaises(ValueError):
            recreated.reply("42", "different prompt", "1")

    def test_interrupted_request_never_repeats_inference(self):
        self.turn.run.side_effect = RuntimeError("synthetic failure")
        with self.assertRaises(RuntimeError):
            self.chat.reply("42", "hello", "1")
        self.assertIn("could not finish", self.chat.reply("42", "hello", "1")["text"])
        self.turn.run.assert_called_once()

    def test_timeout_interrupts_turn_and_rejects_additional_work_until_finished(self):
        entered, release, interrupted = threading.Event(), threading.Event(), threading.Event()
        original = self.turn.run.return_value

        def blocked():
            entered.set()
            release.wait(timeout=3)
            return original

        self.turn.run.side_effect = blocked
        self.turn.interrupt.side_effect = lambda: interrupted.set()
        self.chat.turn_timeout = 0.1
        try:
            with self.assertRaises(TimeoutError):
                self.chat.reply("42", "hello", "1")
            self.assertTrue(entered.is_set())
            self.assertTrue(interrupted.wait(timeout=1))
            with self.assertRaises(BlockingIOError):
                self.chat.reply("43", "hello", "2")
            with self.assertRaises(BlockingIOError):
                self.chat.reset("43")
        finally:
            release.set()
            self.assertTrue(self.chat.lock.acquire(timeout=2))
            self.chat.lock.release()


if __name__ == "__main__":
    unittest.main()
