import json
from http.client import IncompleteRead
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from http_response import SyntheticResponse
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
            error = HTTPError(url, code, description, {}, SyntheticResponse(json.dumps({"ok": False, "error_code": code, "description": description}).encode()))
            with self.subTest(code=code), patch("src.telegram.bot.urlopen", side_effect=error):
                with self.assertRaises(TelegramRejected) as caught:
                    api(token, "sendMessage", {"parse_mode": "HTML"})
                self.assertEqual(caught.exception.formatting, expected)
                self.assertNotIn("synthetic", str(caught.exception))
        for failure in (URLError(url), TimeoutError(url)):
            with patch("src.telegram.bot.urlopen", side_effect=failure), self.assertRaises(RuntimeError) as caught:
                api(token, "sendMessage", {"parse_mode": "HTML"})
            self.assertNotIsInstance(caught.exception, TelegramRejected)
        contradictory = HTTPError(url, 500, "synthetic-private", {}, SyntheticResponse(json.dumps({
            "ok": False, "error_code": 400, "description": "Bad Request: can't parse entities",
        }).encode()))
        with patch("src.telegram.bot.urlopen", side_effect=contradictory), self.assertRaises(RuntimeError) as caught:
            api(token, "sendMessage", {"parse_mode": "HTML"})
        self.assertNotIsInstance(caught.exception, TelegramRejected)

    def test_send_response_is_bounded_but_polling_backlog_keeps_existing_behavior(self):
        def response(body):
            return SyntheticResponse(body)
        for body in (b"not json", b"x" * 262145):
            with patch("src.telegram.bot.urlopen", return_value=response(body)), self.assertRaises(RuntimeError) as caught:
                api("123:synthetic", "sendMessage", {})
            self.assertNotIsInstance(caught.exception, TelegramRejected)
        broken = SyntheticResponse(b"x")
        broken.source.read1 = Mock(side_effect=IncompleteRead(b"synthetic-private"))
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
            error = HTTPError("https://example.invalid", 500, "private", {}, SyntheticResponse(body))
            with self.subTest(body=body[:20]), patch("src.telegram.bot.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    api("123:synthetic", "sendMessage", {"parse_mode": "HTML"})
                self.assertNotIsInstance(caught.exception, TelegramRejected)
        broken = SyntheticResponse(b"x")
        broken.source.read1 = Mock(side_effect=IncompleteRead(b"private"))
        error = HTTPError("https://example.invalid", 400, "private", {}, broken)
        with patch("src.telegram.bot.urlopen", side_effect=error), self.assertRaises(RuntimeError) as caught:
            api("123:synthetic", "sendMessage", {"parse_mode": "HTML"})
        self.assertNotIsInstance(caught.exception, TelegramRejected)
        self.assertNotIn("private", str(caught.exception))

    def test_ordinary_api_responses_are_bounded_without_repeating_requests(self):
        for method in ("sendMessage", "sendChatAction", "getFile", "getMe"):
            response = SyntheticResponse(b"x" * 65538)
            with self.subTest(method=method), patch("src.telegram.bot.urlopen", return_value=response) as opening:
                with self.assertRaisesRegex(RuntimeError, "excessive"):
                    api("123:synthetic", method)
            opening.assert_called_once()
            self.assertTrue(response.closed)
            self.assertEqual(response.body_bytes, 65537)

    def test_polling_shrinks_oversized_batches_without_acknowledging_new_offsets(self):
        oversized = SyntheticResponse(b"x" * 8388609)
        success = SyntheticResponse(b'{"ok":true,"result":[{"update_id":50}]}')
        original = {"offset": 50, "timeout": 25, "allowed_updates": ["message"]}

        def opening(request, timeout):
            self.assertEqual(json.loads(request.data)["offset"], 50)
            if "limit" not in json.loads(request.data):
                return oversized
            self.assertTrue(oversized.closed)
            return success

        with patch("src.telegram.bot.urlopen", side_effect=opening) as opened:
            self.assertEqual(api("123:synthetic", "getUpdates", original), [{"update_id": 50}])
        self.assertEqual([json.loads(call.args[0].data).get("limit", 100) for call in opened.call_args_list], [100, 50])
        self.assertEqual(original, {"offset": 50, "timeout": 25, "allowed_updates": ["message"]})
        self.assertEqual(oversized.body_bytes, 8388609)
        self.assertTrue(success.closed)

    def test_polling_reductions_stop_at_one_and_share_the_original_deadline(self):
        limits = []
        responses = []

        def opening(request, timeout):
            limits.append(json.loads(request.data).get("limit", 100))
            response = SyntheticResponse(b"x" * 8388609)
            responses.append(response)
            return response

        with patch("src.telegram.bot.urlopen", side_effect=opening):
            with self.assertRaisesRegex(RuntimeError, "excessive"):
                api("123:synthetic", "getUpdates", {"offset": 50})
        self.assertEqual(limits, [100, 50, 25, 12, 6, 3, 1])
        self.assertTrue(all(response.closed for response in responses))

        response = SyntheticResponse(b"x" * 8388609)
        read = response.source.read1
        clock = Mock(return_value=0)

        def almost_expired(size):
            chunk = read(size)
            if response.source.raw.source.tell() == len(response.socket.wire):
                clock.side_effect = [38, 39, 40]
            return chunk

        response.source.read1 = almost_expired
        with patch("src.telegram.response_body.time.monotonic", clock), patch(
            "src.telegram.bot.urlopen", return_value=response
        ) as opened:
            with self.assertRaisesRegex(RuntimeError, "network"):
                api("123:synthetic", "getUpdates", {"offset": 50})
        opened.assert_called_once()
        self.assertTrue(response.closed)

    def test_slow_api_success_and_error_bodies_cannot_renew_the_deadline(self):
        bodies = (
            (b'{"ok":true,"result":{}}', False),
            (json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}).encode(), True),
        )
        for body, failed in bodies:
            response = SyntheticResponse(body)
            response.length = None
            read = response.source.read1
            clock = Mock(return_value=0)

            def slow_read(size):
                clock.return_value += 21
                return read(size)

            response.source.read1 = slow_read
            error = HTTPError("https://example.invalid/123:synthetic", 400, "private", {}, response)
            with self.subTest(failed=failed), patch("src.telegram.response_body.time.monotonic", clock), patch(
                "src.telegram.bot.urlopen", side_effect=error if failed else None,
                return_value=response,
            ) as opened:
                with self.assertRaises(RuntimeError) as caught:
                    api("123:synthetic", "sendMessage")
            self.assertNotIsInstance(caught.exception, TelegramRejected)
            self.assertNotIn("synthetic", str(caught.exception))
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual([call.args[0] for call in response.socket.settimeout.call_args_list], [40, 19])
            self.assertTrue(response.closed)
            opened.assert_called_once()

    def test_truncated_json_response_cannot_authorize_send_fallback(self):
        body = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}).encode()
        for failed in (False, True):
            response = SyntheticResponse(body, length=len(body) + 1)
            error = HTTPError("https://example.invalid", 400, "private", {}, response)
            with self.subTest(failed=failed), patch(
                "src.telegram.bot.urlopen", side_effect=error if failed else None, return_value=response
            ):
                with self.assertRaises(RuntimeError) as caught:
                    api("123:synthetic", "sendMessage")
            self.assertNotIsInstance(caught.exception, TelegramRejected)
            self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
