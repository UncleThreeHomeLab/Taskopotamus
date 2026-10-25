import unittest
import time
from unittest.mock import Mock, patch

from src.telegram.bot import process_update, deliver_one
from src.messaging.delivery_store import DeliveryStore as Outbox
from src.telegram.delivery_store import DeliveryStore
from database_support import isolated_database


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.database = isolated_database(self)
        self.store = DeliveryStore(self.database)
        self.agent = Mock()
        self.canonical = Outbox(self.database)
        def chat(chat_id, text, request_id):
            with self.database.connect() as db:
                self.canonical.publish(db, str(chat_id), request_id, [{"type": "text", "text": "x" * 3000}])
            return {"text": "x" * 3000, **self.canonical.get(str(chat_id), request_id)}
        self.agent.chat.side_effect = chat
        self.agent.claim_delivery.side_effect = lambda allowed: self.canonical.claim(sorted(allowed))
        self.agent.acknowledge_delivery.side_effect = lambda delivery, state, receipt: self.canonical.acknowledge(
            delivery["delivery_id"], delivery["sequence"], delivery["attempt"], state, receipt)
        self.agent.import_legacy_delivery.side_effect = lambda chat, request, chunks, next_chunk: self.canonical.import_legacy(str(chat), request, chunks, next_chunk)
        self.agent.reserve_command.side_effect = lambda chat, request, command: self.canonical.reserve_command(str(chat), request, command)
        self.agent.lookup_reply.side_effect = lambda chat, request: self.canonical.get(str(chat), request)
        self.agent.publish_reply.side_effect = lambda chat, request, text, attempt: self.canonical.complete_command(str(chat), request, attempt, text)
        self.update = {"update_id": 10, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "hello",
        }}

    def test_partial_delivery_and_ambiguous_send_never_repeat_after_restart(self):
        with patch("src.telegram.bot.api", return_value={}) as activity:
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.assertTrue(all(call.args[1] == "sendChatAction" for call in activity.call_args_list))
        with patch("src.telegram.bot.api", side_effect=[{"message_id": 1}, RuntimeError("lost acknowledgement")]) as send:
            self.assertTrue(deliver_one(self.agent, "synthetic-token", {"42"}))
            with self.database.connect() as db:
                db.execute("UPDATE delivery_pacing SET not_before=0")
            self.assertTrue(deliver_one(self.agent, "synthetic-token", {"42"}))
            self.assertEqual(send.call_count, 2)
        restarted = Outbox(self.database)
        self.assertIsNone(restarted.claim(["42"]))
        self.assertEqual([row["state"] for row in restarted.status("42")], ["delivered", "uncertain"])
        process_update(self.update, {"42"}, self.agent, "synthetic-token", DeliveryStore(self.database))
        self.agent.chat.assert_called_once_with(42, "hello", "10")
        self.assertEqual(self.store.offset, 11)

    def test_legacy_pending_moves_to_canonical_quarantine_without_inference(self):
        self.store.prepare(10, 42, ["x" * 2000, "y" * 1000])
        self.store.mark_sent(10, 1)
        process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.agent.chat.assert_not_called()
        self.assertEqual([r["state"] for r in self.canonical.status("42")], ["delivered", "uncertain"])
        self.assertIsNone(self.store.get(10))

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

    def test_volatile_command_reuses_committed_reply_after_lost_publish_ack(self):
        self.update["message"]["text"] = "/delivery_status"
        self.agent.delivery_status.side_effect = [[], [{"state": "delivered"}]]
        def committed_but_ack_lost(chat, request, text, attempt):
            self.canonical.complete_command(str(chat), request, attempt, text)
            raise RuntimeError("synthetic lost commit acknowledgement")
        self.agent.publish_reply.side_effect = committed_but_ack_lost
        with self.assertRaises(RuntimeError):
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        committed = self.canonical.get("42", "10")
        process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.assertEqual(self.canonical.get("42", "10"), committed)
        self.agent.delivery_status.assert_called_once_with(42)
        self.agent.publish_reply.assert_called_once()
        self.assertEqual(self.store.offset, 11)

    def test_reset_is_not_repeated_after_poll_offset_commit_failure(self):
        self.update["message"]["text"] = "/new"
        self.agent.reset.return_value = {"text": "New conversation started."}
        with patch.object(self.store, "finish", side_effect=RuntimeError("synthetic offset commit failure")):
            with self.assertRaises(RuntimeError):
                process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.agent.reset.assert_called_once_with(42)
        self.assertEqual(self.store.offset, 11)

    def test_unknown_local_effect_is_reported_without_repeating_login_retry(self):
        self.update["message"]["text"] = "/codex_login retry"
        self.agent.login.return_value = {"state": "signed_in"}
        self.agent.publish_reply.side_effect = RuntimeError("synthetic failure before output commit")
        with self.assertRaises(RuntimeError):
            process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        process_update(self.update, {"42"}, self.agent, "synthetic-token", self.store)
        self.agent.login.assert_called_once_with(restart=True)
        self.assertEqual(self.store.offset, 11)
        notice = self.canonical.get("42", "10")
        self.assertEqual(notice["reply_status"], "failed")
        self.assertIn("uncertain", notice["items"][0]["text"])


if __name__ == "__main__":
    unittest.main()
