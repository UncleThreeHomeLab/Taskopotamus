import unittest
from unittest.mock import Mock, patch

from src.telegram.bot import process_update
from src.telegram.bot import TelegramSendError
from src.telegram.delivery_store import DeliveryStore
from src.telegram.formatting import format_reply
from database_support import isolated_database


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.database = isolated_database(self)
        self.store = DeliveryStore(self.database)
        self.agent = Mock()
        self.agent.chat.return_value = {"text": "**" + "x" * 6000 + "**"}
        self.update = {"update_id": 10, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "hello",
        }}

    def test_partial_send_retries_saved_reply_after_restart_without_another_model_call(self):
        with patch("src.telegram.bot.api", side_effect=[{}, TelegramSendError("transient")]):
            with self.assertRaises(RuntimeError):
                process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        restarted = DeliveryStore(self.database)
        saved = restarted.get(10)
        with patch("src.telegram.bot.api", return_value={}) as send:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", restarted)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[2]["text"], saved[1][1]["text"])
            self.assertEqual(send.call_args.args[2]["parse_mode"], "HTML")
        self.agent.chat.assert_called_once_with(42, "hello", "10")
        self.assertEqual(restarted.offset, 11)
        self.assertIsNone(restarted.get(10))
        process_update(self.update, {"42"}, self.agent, "synthetic-token", restarted)
        self.agent.chat.assert_called_once()

    def test_uncertain_send_retains_tail_and_later_updates_continue_without_duplicate(self):
        with patch("src.telegram.bot.api", side_effect=TelegramSendError("uncertain")) as send:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.assertEqual(send.call_count, 1)
        saved = self.store.get(10)
        self.assertEqual(saved[2], 0)
        self.assertEqual(saved[1][0]["state"], "uncertain")
        self.assertEqual(len(saved[1]), 2)
        self.assertEqual(self.store.offset, 11)
        with patch("src.telegram.bot.api", return_value={}) as later:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
            later.assert_not_called()
            self.agent.chat.return_value = {"text": "next reply"}
            process_update({**self.update, "update_id": 11}, {"42"}, self.agent, "synthetic-token", self.store)
            later.assert_called_once()
        self.assertEqual(self.store.offset, 12)
        self.assertIsNotNone(self.store.get(10))
        self.assertIsNone(self.store.get(11))

    def test_crash_after_sending_marker_or_progress_failure_never_resends(self):
        self.store.prepare(10, 42, format_reply("saved reply"))
        self.store.mark_sending(10, 0, self.store.get(10)[1][0])
        with patch("src.telegram.bot.api") as send:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", DeliveryStore(self.database))
        send.assert_not_called()
        self.assertEqual(self.store.get(10)[1][0]["state"], "uncertain")
        self.assertEqual(self.store.offset, 11)
        self.agent.chat.return_value = {"text": "next"}
        update = {**self.update, "update_id": 11}
        with patch.object(self.store, "mark_sent", side_effect=RuntimeError("synthetic write failure")), patch("src.telegram.bot.api", return_value={}) as first:
            with self.assertRaises(RuntimeError):
                process_update(update, {"42"}, self.agent, "synthetic-token", self.store)
        first.assert_called_once()
        self.assertEqual(self.store.get(11)[1][0]["state"], "sending")
        with patch("src.telegram.bot.api") as retry:
            process_update(update, {"42"}, self.agent, "synthetic-token", DeliveryStore(self.database))
        retry.assert_not_called()
        self.assertEqual(self.store.get(11)[1][0]["state"], "uncertain")

    def test_formatting_rejection_persists_plain_fallback_before_sending(self):
        self.agent.chat.return_value = {"text": "**Read** [details](https://example.com?a=1&b=2)"}
        seen = []

        def send(_token, _method, body):
            saved = self.store.get(10)[1][0]
            self.assertEqual(saved["state"], "sending")
            self.assertEqual(saved["text"], body["text"])
            self.assertTrue(body["protect_content"])
            self.assertTrue(body["link_preview_options"]["is_disabled"])
            seen.append(body)
            if len(seen) == 1:
                raise TelegramSendError("formatting")
            self.assertIsNone(saved["parse_mode"])
            self.assertNotIn("parse_mode", body)
            self.assertIn("https://example.com?a=1&b=2", body["text"])
            return {}

        with patch("src.telegram.bot.api", side_effect=send):
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.store.offset, 11)
        self.agent.chat.assert_called_once()

    def test_fallback_saved_before_crash_resumes_plain_without_model(self):
        self.agent.chat.return_value = {"text": "**saved**"}
        mark = self.store.mark_sending
        attempts = []

        def mark_until_crash(*args):
            attempts.append(args)
            if len(attempts) == 2:
                raise RuntimeError("synthetic database failure")
            mark(*args)

        with patch.object(self.store, "mark_sending", side_effect=mark_until_crash), patch("src.telegram.bot.api", side_effect=TelegramSendError("formatting")):
            with self.assertRaises(RuntimeError):
                process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        saved = self.store.get(10)[1][0]
        self.assertIsNone(saved["parse_mode"])
        self.assertEqual(saved["state"], "pending")
        with patch("src.telegram.bot.api", return_value={}) as send:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", DeliveryStore(self.database))
        self.assertNotIn("parse_mode", send.call_args.args[2])
        self.assertEqual(send.call_args.args[2]["text"], "saved")
        self.agent.chat.assert_called_once()

    def test_quarantine_cursor_and_terminal_state_are_atomic(self):
        with self.database.connect() as db:
            db.execute('ALTER TABLE polling ADD CONSTRAINT frozen_offset CHECK ("offset" = 0)')
        with patch("src.telegram.bot.api", side_effect=TelegramSendError("uncertain")) as send:
            with self.assertRaises(Exception):
                process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        send.assert_called_once()
        self.assertEqual(self.store.offset, 0)
        self.assertEqual(self.store.get(10)[1][0]["state"], "sending")
        with self.database.connect() as db:
            db.execute('ALTER TABLE polling DROP CONSTRAINT frozen_offset')
        with patch("src.telegram.bot.api") as recovered:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", DeliveryStore(self.database))
        recovered.assert_not_called()
        self.assertEqual(self.store.offset, 11)
        self.assertEqual(self.store.get(10)[1][0]["state"], "uncertain")

    def test_permanent_rejection_is_retained_without_poisoning_polling(self):
        with patch("src.telegram.bot.api", side_effect=TelegramSendError("rejected")) as send:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        send.assert_called_once()
        self.assertEqual(self.store.get(10)[1][0]["state"], "rejected")
        self.assertEqual(self.store.offset, 11)

    def test_legacy_plain_chunks_are_normalized_and_never_interpreted_as_html(self):
        with self.database.connect() as db:
            db.execute('INSERT INTO pending VALUES (10, 42, %s, 1)', ('["already sent", "**literal** <b>text</b>"]',))
        with patch("src.telegram.bot.api", return_value={}) as send:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.assertEqual(send.call_args.args[2]["text"], "**literal** <b>text</b>")
        self.assertNotIn("parse_mode", send.call_args.args[2])
        self.agent.chat.assert_not_called()

    def test_corrupt_saved_delivery_fails_closed_without_sending(self):
        with self.database.connect() as db:
            db.execute('INSERT INTO pending VALUES (10, 42, %s, 0)', ('[{"text": "missing state"}]',))
        with patch("src.telegram.bot.api") as send:
            with self.assertRaises(ValueError):
                process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        send.assert_not_called()
        self.agent.chat.assert_not_called()

    def test_disallowed_user_does_not_create_reply_or_call_services(self):
        with patch("src.telegram.bot.api") as send:
            process_update(self.update, {"99"}, self.agent, "synthetic-token", self.store)
        self.agent.chat.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.store.offset, 11)

    def test_revoked_user_does_not_receive_previously_cached_reply(self):
        self.store.prepare(10, 42, ["synthetic private reply"])
        with patch("src.telegram.bot.api") as send:
            process_update(self.update, {"99"}, self.agent, "synthetic-token", self.store)
        send.assert_not_called()
        self.assertIsNone(self.store.get(10))


if __name__ == "__main__":
    unittest.main()
