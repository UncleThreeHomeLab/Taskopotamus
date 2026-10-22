import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.telegram.bot import process_update
from src.telegram.delivery_store import DeliveryStore


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "delivery.sqlite3"
        self.store = DeliveryStore(self.database)
        self.agent = Mock()
        self.agent.chat.return_value = {"text": "x" * 3000}
        self.update = {"update_id": 10, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "hello",
        }}

    def test_partial_send_retries_saved_reply_after_restart_without_another_model_call(self):
        with patch("src.telegram.bot.api", side_effect=[{}, RuntimeError("synthetic delivery failure")]):
            with self.assertRaises(RuntimeError):
                process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        restarted = DeliveryStore(self.database)
        with patch("src.telegram.bot.api", return_value={}) as send:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", restarted)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[2]["text"], "x" * 1000)
        self.agent.chat.assert_called_once_with(42, "hello", "10")
        self.assertEqual(restarted.offset, 11)
        self.assertIsNone(restarted.get(10))
        process_update(self.update, {"42"}, self.agent, "synthetic-token", restarted)
        self.agent.chat.assert_called_once()

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
