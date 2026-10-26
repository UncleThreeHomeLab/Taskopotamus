import io
import json
from http.client import IncompleteRead
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from src.telegram.bot import api, read_config, reply_chunks, reply_for
from src.telegram.media import TelegramRejected


class BotTests(unittest.TestCase):
    def test_configuration(self):
        for env in ({}, {"TELEGRAM_BOT_TOKEN": "123:synthetic"}, {
            "TELEGRAM_BOT_TOKEN": "123:synthetic", "TELEGRAM_ALLOWED_USER_IDS": "42,",
        }):
            with self.subTest(env=env), self.assertRaises(ValueError):
                read_config(env)
        self.assertEqual(read_config({
            "TELEGRAM_BOT_TOKEN": "123:synthetic", "TELEGRAM_ALLOWED_USER_IDS": "42, 43",
        }), ("123:synthetic", {"42", "43"}))

    def test_only_allowed_private_senders_receive_replies(self):
        message = {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/ping"}
        self.assertEqual(reply_for(message, {"42"}), "pong")
        for rejected in (
            {**message, "from": {"id": 99}},
            {**message, "chat": {"id": -1, "type": "group"}},
            {**message, "from": {}},
            {**message, "text": None},
            None,
        ):
            with self.subTest(message=rejected):
                self.assertIsNone(reply_for(rejected, {"42"}))
        self.assertIn("not configured", reply_for({**message, "text": "hello"}, {"42"}))
        self.assertIn("Taskopotamus", reply_for({**message, "text": "/start"}, {"42"}))

    def test_network_errors_do_not_expose_credentials(self):
        token = "123:synthetic"
        url = f"https://api.telegram.org/bot{token}/getMe"
        for error in (HTTPError(url, 401, url, {}, None), URLError(url), TimeoutError(url)):
            with self.subTest(error=type(error).__name__):
                with patch("src.telegram.bot.urlopen", side_effect=error):
                    with self.assertRaises(RuntimeError) as caught:
                        api(token, "getMe")
                self.assertNotIn(token, str(caught.exception))

    def test_login_commands_require_allowed_private_sender(self):
        agent = Mock()
        agent.login.return_value = {
            "state": "pending", "verification_url": "https://auth.openai.com/codex/device",
            "user_code": "TEST-0000",
        }
        message = {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/codex_login"}
        self.assertIsNone(reply_for(message, {"99"}, agent))
        self.assertIsNone(reply_for({**message, "chat": {"type": "group"}}, {"42"}, agent))
        agent.login.assert_not_called()
        self.assertIn("TEST-0000", reply_for(message, {"42"}, agent))
        self.assertIn("TEST-0000", reply_for({**message, "text": "/codex_login retry"}, {"42"}, agent))
        agent.login.assert_called_with(restart=True)
        agent.status.return_value = {"state": "signed_in"}
        self.assertEqual(reply_for({**message, "text": "/codex_status"}, {"42"}, agent), "Codex is signed in.")
        agent.login.side_effect = RuntimeError("synthetic-sensitive-detail")
        self.assertNotIn("synthetic-sensitive-detail", reply_for(message, {"42"}, agent))

    def test_chat_and_reset_are_only_forwarded_for_allowed_private_users(self):
        agent = Mock()
        agent.chat.return_value = {"text": "Hi there"}
        agent.reset.return_value = {"text": "New conversation started."}
        message = {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "hello"}
        self.assertIsNone(reply_for(message, {"99"}, agent))
        agent.chat.assert_not_called()
        self.assertEqual(reply_for(message, {"42"}, agent), "Hi there")
        agent.chat.assert_called_once_with(42, "hello", None)
        self.assertEqual(reply_for({**message, "text": "/new"}, {"42"}, agent), "New conversation started.")
        agent.reset.assert_called_once_with(42)
        agent.chat.side_effect = RuntimeError("Codex is signed out. Send /codex_login.")
        self.assertIn("/codex_login", reply_for(message, {"42"}, agent))

    def test_long_unicode_replies_are_split_without_losing_text(self):
        text = "hello\n" + "\U0001f99b" * 5000
        chunks = reply_chunks(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks))

    def test_send_errors_use_only_reliable_format_rejections_and_sanitized_diagnostics(self):
        token = "123:synthetic"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        cases = [(400, "Bad Request: can't parse entities: synthetic-private", True),
                 (400, "Bad Request: message is too long", False),
                 (429, "Too many requests", False), (500, "server error", False)]
        for code, description, expected in cases:
            error = HTTPError(url, code, description, {}, io.BytesIO(json.dumps({"ok": False, "error_code": code, "description": description}).encode()))
            with self.subTest(code=code), patch("src.telegram.bot.urlopen", side_effect=error):
                with self.assertRaises(TelegramRejected) as caught:
                    api(token, "sendMessage", {"parse_mode": "HTML"})
                self.assertEqual(caught.exception.formatting, expected)
                self.assertNotIn("synthetic", str(caught.exception))
        for failure in (URLError(url), TimeoutError(url)):
            with patch("src.telegram.bot.urlopen", side_effect=failure), self.assertRaises(RuntimeError) as caught:
                api(token, "sendMessage", {"parse_mode": "HTML"})
            self.assertNotIsInstance(caught.exception, TelegramRejected)
        contradictory = HTTPError(url, 500, "synthetic-private", {}, io.BytesIO(json.dumps({
            "ok": False, "error_code": 400, "description": "Bad Request: can't parse entities",
        }).encode()))
        with patch("src.telegram.bot.urlopen", side_effect=contradictory), self.assertRaises(RuntimeError) as caught:
            api(token, "sendMessage", {"parse_mode": "HTML"})
        self.assertNotIsInstance(caught.exception, TelegramRejected)

    def test_send_response_is_bounded_but_polling_backlog_keeps_existing_behavior(self):
        def response(body):
            result = Mock()
            result.__enter__ = Mock(return_value=io.BytesIO(body))
            result.__exit__ = Mock(return_value=False)
            return result
        for body in (b"not json", b"x" * 262145):
            with patch("src.telegram.bot.urlopen", return_value=response(body)), self.assertRaises(RuntimeError) as caught:
                api("123:synthetic", "sendMessage", {})
            self.assertNotIsInstance(caught.exception, TelegramRejected)
        broken = Mock()
        broken.__enter__ = Mock(return_value=Mock(read=Mock(side_effect=IncompleteRead(b"synthetic-private"))))
        broken.__exit__ = Mock(return_value=False)
        with patch("src.telegram.bot.urlopen", return_value=broken), self.assertRaises(RuntimeError) as caught:
            api("123:synthetic", "sendMessage", {})
        self.assertNotIsInstance(caught.exception, TelegramRejected)
        self.assertNotIn("synthetic-private", str(caught.exception))
        with patch("src.telegram.bot.urlopen", return_value=response(b'{"ok":true,"result":{"message_id":1}}')):
            self.assertEqual(api("123:synthetic", "sendMessage", {})["message_id"], 1)
        rejection = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}).encode()
        for mode in ("HTML", None):
            with patch("src.telegram.bot.urlopen", return_value=response(rejection)), self.assertRaises(TelegramRejected) as caught:
                api("123:synthetic", "sendMessage", {"parse_mode": mode})
            self.assertTrue(caught.exception.formatting)
        backlog = [{"synthetic": "x" * 300000}]
        with patch("src.telegram.bot.urlopen", return_value=response(json.dumps({"ok": True, "result": backlog}).encode())):
            self.assertEqual(api("123:synthetic", "getUpdates"), backlog)

    def test_malformed_or_mismatched_http_rejections_never_authorize_fallback(self):
        for body in (b"invalid", b"x" * 65537, b"[]", json.dumps({
            "ok": False, "error_code": 400, "description": "Bad Request: can't parse entities",
        }).encode()):
            error = HTTPError("https://example.invalid", 500, "private", {}, io.BytesIO(body))
            with self.subTest(body=body[:20]), patch("src.telegram.bot.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    api("123:synthetic", "sendMessage", {"parse_mode": "HTML"})
                self.assertNotIsInstance(caught.exception, TelegramRejected)
        broken = Mock(read=Mock(side_effect=IncompleteRead(b"private")))
        error = HTTPError("https://example.invalid", 400, "private", {}, broken)
        with patch("src.telegram.bot.urlopen", side_effect=error), self.assertRaises(RuntimeError) as caught:
            api("123:synthetic", "sendMessage", {"parse_mode": "HTML"})
        self.assertNotIsInstance(caught.exception, TelegramRejected)
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
