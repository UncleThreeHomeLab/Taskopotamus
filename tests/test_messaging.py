import hashlib
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from database_support import isolated_database
from src.agent.chat_service import ChatService
from src.agent.http_server import AgentHTTPServer
from src.agent.server import make_handler
from src.messaging.artifacts import ArtifactCapacityError, ArtifactPolicy, ArtifactStore
from src.messaging.delivery_store import DeliveryStore
from src.messaging.http_api import MessagingAPI
from src.messaging.message_publication import MessagePublication
from src.telegram.agent_client import AgentClient
from src.telegram.bot import process_update
from src.telegram.delivery_store import DeliveryStore as PollingStore


TOKEN = "synthetic-messaging-service-token-only"


class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.database = isolated_database(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.policy = ArtifactPolicy(max_files=2, input_max_bytes=64, output_max_bytes=64, image_max_bytes=32,
                                     capacity_bytes=100, staging_seconds=100, retention_seconds=1000, sweep_seconds=1)
        self.artifacts = ArtifactStore(self.database, self.directory.name, self.policy, self.clock)
        self.deliveries = DeliveryStore(self.database, retention=1000, attempt_timeout=10, clock=self.clock)

    def upload(self, content=b"synthetic-data", chat="42", request="1", direction="output", operation="file", capability=None):
        return self.artifacts.upload(chat, request, direction, operation, "result.txt", "text/plain", len(content),
                                     hashlib.sha256(content).hexdigest(), io.BytesIO(content), capability)

    def publish(self, chat="42", request="1", text="hello"):
        with self.database.connect() as db:
            return self.deliveries.publish(db, chat, request, [{"type": "text", "text": text}])

    def test_artifact_snapshot_request_isolation_and_revocation(self):
        capability = self.artifacts.begin_run("42", "1", 60)
        info = self.upload(capability=capability)
        self.assertEqual(self.upload(capability=capability), info)
        with self.assertRaises(ValueError):
            self.upload(b"changed", capability=capability)
        for chat, request in (("43", "1"), ("42", "2")):
            with self.assertRaises(PermissionError):
                self.artifacts.describe(info["artifact_id"], chat, request)
        items = [{"type": "file", "artifact_id": info["artifact_id"], "caption": "Description"}]
        self.assertEqual(self.artifacts.prepare_batch(capability, "batch", items)["items"], items)
        self.assertEqual(self.artifacts.prepare_batch(capability, "batch", items)["version"], 1)
        with self.database.connect() as db:
            published = self.artifacts.publish(db, "42", "1")
            self.deliveries.publish(db, "42", "1", published)
        with self.assertRaises(PermissionError):
            self.artifacts.scope(capability)
        self.now += 101
        self.artifacts.maintain()
        info, file = self.artifacts.open(info["artifact_id"], "42", committed=True)
        with file:
            self.assertEqual(file.read(), b"synthetic-data")
        self.assertEqual(info["state"], "committed")
        self.now += 1000
        with self.database.connect() as db:
            self.deliveries.maintain(db)
        self.artifacts.maintain()
        self.assertEqual(self.deliveries.status("42")[0]["state"], "expired")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_upload_capacity_actual_digest_names_and_abandoned_cleanup(self):
        with self.assertRaises(ValueError):
            self.artifacts.upload("42", "1", "input", "bad", "../secret", "text/plain", 1, "a" * 64, io.BytesIO(b"x"))
        with self.assertRaises(ValueError):
            self.artifacts.upload("42", "1", "input", "bad", "x", "text/plain", 5, "a" * 64, io.BytesIO(b"x"))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.upload(b"x" * 60)
        with self.assertRaises(ArtifactCapacityError):
            self.upload(b"x" * 60, chat="43")
        self.now += 101
        self.artifacts.maintain()
        self.upload(b"x" * 60, chat="43")

    def test_batch_failure_does_not_publish_partial_attachments(self):
        cap = self.artifacts.begin_run("42", "1", 60)
        first = self.upload(capability=cap)
        with self.assertRaises(PermissionError):
            self.artifacts.prepare_batch(cap, "batch", [{"type": "file", "artifact_id": first["artifact_id"]},
                                                        {"type": "file", "artifact_id": "f" * 32}])
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attachment_batches").fetchone()[0], 0)
            self.artifacts.end_run(db, "42", "1", "failed")
        with self.assertRaises(PermissionError):
            self.artifacts.prepare_batch(cap, "batch", [{"type": "file", "artifact_id": first["artifact_id"]}])

    def test_delivery_order_receipt_retries_and_late_confirmation(self):
        self.publish(text="x" * 5000)
        first = self.deliveries.claim(["42"])
        self.assertIsNone(self.deliveries.claim(["42"]))
        args = (first["delivery_id"], first["sequence"], first["attempt"], "delivered", {"message_id": 7})
        self.deliveries.acknowledge(*args)
        self.deliveries.acknowledge(*args)
        self.now += 1
        second = self.deliveries.claim(["42"])
        self.now += 11
        self.assertIsNone(self.deliveries.claim(["42"]))
        self.assertEqual(self.deliveries.status("42")[1]["state"], "uncertain")
        self.deliveries.acknowledge(second["delivery_id"], second["sequence"], second["attempt"], "delivered", {"message_id": 8})
        self.now += 1
        self.assertEqual(self.deliveries.claim(["42"])["sequence"], 2)
        self.assertEqual(self.deliveries.status("43"), [])
        with self.assertRaises(ValueError):
            self.deliveries.acknowledge(first["delivery_id"], 0, "f" * 32, "delivered", {"message_id": 9})

    def test_legacy_import_idempotency_collision_and_revocation(self):
        self.deliveries.import_legacy("42", "1", ["a" * 2000, "b"], 1)
        self.deliveries.import_legacy("42", "1", ["a" * 2000, "b"], 1)
        self.assertEqual([x["state"] for x in self.deliveries.status("42")], ["delivered", "uncertain"])
        with self.assertRaises(ValueError):
            self.deliveries.import_legacy("42", "1", ["different"], 0)
        self.publish(request="2")
        self.assertIsNone(self.deliveries.claim(["43"]))

    def test_canonical_publish_rollback_does_not_expose_reply(self):
        with self.assertRaises(RuntimeError):
            with self.database.connect() as db:
                self.deliveries.publish(db, "42", "1", [{"type": "text", "text": "private pending"}])
                raise RuntimeError("synthetic commit failure")
        self.assertIsNone(self.deliveries.get("42", "1"))

    def test_pending_appends_deliver_early_and_preserve_operation_and_receipt_identity(self):
        first_items = [{"type": "text", "text": "Working"}]
        with self.database.connect() as db:
            identifier = self.deliveries.append(db, "42", "1", "update-1", first_items)
        self.assertIsNone(self.deliveries.get("42", "1"))
        self.assertEqual(self.deliveries.snapshot("42", "1")["reply_status"], "pending")
        first = self.deliveries.claim(["42"])
        self.assertEqual(first["item"], first_items[0])
        self.deliveries.acknowledge(identifier, first["sequence"], first["attempt"], "delivered", {"message_id": 1})
        with self.database.connect() as db:
            self.deliveries.append(db, "42", "1", "update-2", [{"type": "text", "text": "Result"}])
            self.assertEqual(self.deliveries.append(db, "42", "1", "update-1", first_items), identifier)
            self.deliveries.finish(db, "42", "1")
        self.now += 2
        second = self.deliveries.claim(["42"])
        self.assertEqual((second["delivery_id"], second["sequence"], second["item"]["text"]), (identifier, 1, "Result"))
        with self.database.connect() as db:
            self.assertEqual(self.deliveries.append(db, "42", "1", "update-1", first_items), identifier)
        for key, items in (("update-1", [{"type": "text", "text": "changed"}]),
                           ("update-3", first_items), ("application:terminal", first_items)):
            with self.assertRaises(ValueError), self.database.connect() as db:
                self.deliveries.append(db, "42", "1", key, items)
        self.assertIsNone(self.deliveries.snapshot("43", "1"))
        self.assertEqual(self.deliveries.status("42")[0]["state"], "delivered")

    def test_pending_limits_reserve_atomic_terminal_failure_and_expired_envelope_stays_closed(self):
        progress = [{"type": "text", "text": "x" * 62946}] + [{"type": "text", "text": "x"}] * 30
        with self.assertRaises(ValueError), self.database.connect() as db:
            self.deliveries.append(db, "42", "3", "oversized", [{"type": "text", "text": "x" * 62977}])
        self.assertIsNone(self.deliveries.snapshot("42", "3"))
        with self.database.connect() as db:
            self.deliveries.append(db, "42", "1", "progress", progress)
        for key, items in (("extra", [{"type": "text", "text": "more"}]),
                           ("extra", [{"type": "text", "text": "x" * 1025}])):
            with self.assertRaises(ValueError), self.database.connect() as db:
                self.deliveries.append(db, "42", "1", key, items)
        with self.assertRaises(ValueError), self.database.connect() as db:
            self.deliveries.publish(db, "42", "1", [{"type": "text", "text": "x" * 1025}])
        notice = [{"type": "text", "text": "Application could not complete this request."}]
        with self.database.connect() as db:
            self.deliveries.publish(db, "42", "1", notice, outcome="failed")
        with self.database.connect() as db:
            self.deliveries.publish(db, "42", "1", notice, outcome="failed")
        reply = self.deliveries.get("42", "1")
        self.assertEqual((reply["reply_status"], len(reply["items"])), ("failed", 32))
        self.assertLessEqual(sum(len(i["text"]) for i in reply["items"]), 64000)
        with self.database.connect() as db:
            self.deliveries.append(db, "42", "2", "first", [{"type": "text", "text": "pending"}])
        self.now += 1001
        with self.assertRaises(ValueError), self.database.connect() as db:
            self.deliveries.append(db, "42", "2", "second", notice)
        self.assertEqual(self.deliveries.snapshot("42", "2")["items"], [{"type": "text", "text": "pending"}])

    def chat(self, timeout=180):
        codex, thread, turn = Mock(), Mock(), Mock()
        codex.account.return_value = SimpleNamespace(account={"synthetic": True})
        codex.thread_start.return_value = thread
        thread.id = "synthetic-thread"
        thread.turn.return_value = turn
        turn.run.return_value = SimpleNamespace(status=SimpleNamespace(value="completed"), final_response="done", usage=None)
        chat = ChatService(lambda *_: nullcontext(codex), self.database, turn_timeout=timeout, artifacts=self.artifacts, deliveries=self.deliveries)
        return chat, turn

    def test_expired_pending_failure_closes_without_reviving_messages_or_receipts(self):
        progress = [{"type": "text", "text": "Already sent"}]
        with self.database.connect() as db:
            identifier = self.deliveries.append(db, "42", "1", "first", progress)
        first = self.deliveries.claim(["42"])
        receipt = {"message_id": 11}
        self.deliveries.acknowledge(identifier, 0, first["attempt"], "delivered", receipt)
        with self.database.connect() as db:
            self.deliveries.append(db, "42", "1", "second", [{"type": "text", "text": "Unsent update"}])
        expires = self.deliveries.snapshot("42", "1")["expires_at"]
        self.now += 1001
        with self.assertRaises(ValueError), self.database.connect() as db:
            self.deliveries.finish(db, "42", "1")
        notice = [{"type": "text", "text": "Work could not finish before the messages expired."}]
        for _ in range(2):
            with self.database.connect() as db:
                self.assertEqual(self.deliveries.publish(db, "42", "1", notice, outcome="failed"), identifier)
        reply = self.deliveries.get("42", "1")
        self.assertEqual((reply["reply_status"], reply["expires_at"], len(reply["items"])), ("failed", expires, 3))
        with self.database.connect() as db:
            rows = db.execute("SELECT state,receipt FROM outgoing_items WHERE reply_id=%s ORDER BY sequence", (identifier,)).fetchall()
            self.assertEqual(rows, [("delivered", json.dumps(receipt)), ("expired", None), ("expired", None)])
            self.assertEqual(self.deliveries.append(db, "42", "1", "first", progress), identifier)
        with self.assertRaises(ValueError), self.database.connect() as db:
            self.deliveries.append(db, "42", "1", "late", notice)
        self.assertIsNone(self.deliveries.claim(["42"]))

    def test_expired_abandoned_progress_reconciles_once_without_inference(self):
        chat, turn = self.chat()
        with self.database.connect() as db:
            db.execute("INSERT INTO replies VALUES ('42','1',%s,NULL)", (hashlib.sha256(b"hello").hexdigest(),))
            identifier = self.deliveries.append(db, "42", "1", "progress", [{"type": "text", "text": "Working"}])
        expires = self.deliveries.snapshot("42", "1")["expires_at"]
        self.now += 1001
        chat.deliveries = DeliveryStore(self.database, retention=1000, clock=self.clock)
        result = chat.reply("42", "hello", "1", include_details=True)
        self.assertEqual((result["status"], result["delivery_id"], result["expires_at"]), ("failed", identifier, expires))
        self.assertIn("interrupted", result["items"][-1]["text"])
        self.assertEqual(chat.reply("42", "hello", "1", include_details=True), result)
        self.assertEqual([i["state"] for i in chat.deliveries.status("42")], ["expired", "expired"])
        self.assertIsNone(chat.deliveries.claim(["42"]))
        turn.run.assert_not_called()

    def test_failure_after_progress_expiry_finalizes_without_repeating_inference(self):
        chat, turn = self.chat()
        expires = []

        def run():
            with self.database.connect() as db:
                self.deliveries.append(db, "42", "1", "progress", [{"type": "text", "text": "Working"}])
            expires.append(self.deliveries.snapshot("42", "1")["expires_at"])
            self.now += 1001
            raise RuntimeError("Synthetic inference failure")

        turn.run.side_effect = run
        with self.assertRaises(RuntimeError):
            chat.reply("42", "hello", "1")
        result = chat.reply("42", "hello", "1", include_details=True)
        self.assertEqual((result["status"], result["expires_at"]), ("failed", expires[0]))
        self.assertIn("could not finish", result["items"][-1]["text"])
        self.assertEqual(chat.reply("42", "hello", "1", include_details=True), result)
        self.assertEqual([i["state"] for i in self.deliveries.status("42")], ["expired", "expired"])
        self.assertIsNone(self.deliveries.claim(["42"]))
        turn.run.assert_called_once()

    def test_abandoned_full_progress_is_finalized_without_another_inference(self):
        chat, turn = self.chat()
        progress = [{"type": "text", "text": "x" * 62946}] + [{"type": "text", "text": "x"}] * 30
        with self.database.connect() as db:
            db.execute("INSERT INTO replies VALUES ('42','1',%s,NULL)", (hashlib.sha256(b"hello").hexdigest(),))
            self.deliveries.append(db, "42", "1", "progress", progress)
        result = chat.reply("42", "hello", "1", include_details=True)
        self.assertEqual((result["status"], len(result["items"])), ("failed", 32))
        self.assertEqual(result["items"][:31], progress)
        self.assertIn("interrupted", result["items"][-1]["text"])
        self.assertEqual(chat.reply("42", "hello", "1", include_details=True), result)
        turn.run.assert_not_called()

    def test_native_final_is_ignored_after_full_queued_progress(self):
        chat, turn = self.chat()
        response = turn.run.return_value
        response.final_response = "x" * 1025
        progress = [{"type": "text", "text": "x" * 62946}] + [{"type": "text", "text": "x"}] * 30

        def run():
            with self.database.connect() as db:
                self.deliveries.append(db, "42", "1", "progress", progress)
            return response

        turn.run.side_effect = run
        result = chat.reply("42", "hello", "1", include_details=True)
        self.assertEqual((result["status"], len(result["items"])), ("completed", 31))
        self.assertEqual(result["items"], progress)
        self.assertEqual(chat.reply("42", "hello", "1", include_details=True), result)
        turn.run.assert_called_once()

    def test_scoped_messages_hide_native_final_and_replay_visible_text(self):
        chat, turn = self.chat()
        response = turn.run.return_value
        response.final_response = "INTERNAL FINAL MUST NEVER LEAK"
        publisher = MessagePublication(self.artifacts, self.deliveries)

        def run():
            cap = issued[0]
            publisher.send(cap, "progress", "a" * 64, [{"type": "text", "text": "Working"}])
            self.assertIsNone(self.deliveries.get("42", "1"))
            self.assertEqual(self.deliveries.claim(["42"])["item"]["text"], "Working")
            publisher.send(cap, "result", "b" * 64, [{"type": "text", "text": "Answer"}])
            return response

        issued = []
        original = self.artifacts.begin_run

        def begin(*args, **kwargs):
            token = original(*args, **kwargs)
            issued.append(token)
            return token

        turn.run.side_effect = run
        with patch.object(self.artifacts, "begin_run", side_effect=begin):
            result = chat.reply("42", "hello", "1", include_details=True)
        self.assertEqual(result["text"], "Working\n\nAnswer")
        self.assertEqual([i["text"] for i in result["items"]], ["Working", "Answer"])
        self.assertNotIn("INTERNAL", json.dumps(result))
        self.assertEqual(chat.reply("42", "hello", "1", include_details=True), result)
        turn.run.assert_called_once()

    def test_image_only_empty_native_final_and_legacy_mixed_suppression(self):
        from PIL import Image

        self.artifacts.policy = ArtifactPolicy(output_max_bytes=10000, image_max_bytes=10000, capacity_bytes=100000)
        chat, turn = self.chat()
        response = turn.run.return_value
        response.final_response = ""
        content = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(content, format="PNG")
        image = content.getvalue()

        def run():
            cap = issued[0]
            legacy = self.upload(b"legacy", operation="legacy", capability=cap)
            self.artifacts.prepare_batch(cap, "legacy", [{"type": "file", "artifact_id": legacy["artifact_id"]}])
            info = self.artifacts.upload("42", "1", "output", "image", "image.png", "image/png", len(image),
                                         hashlib.sha256(image).hexdigest(), io.BytesIO(image), cap)
            MessagePublication(self.artifacts, self.deliveries).send(cap, "photo", "c" * 64,
                [{"type": "image", "artifact_id": info["artifact_id"], "caption": "Only this image"}])
            return response

        issued, original = [], self.artifacts.begin_run

        def begin(*args, **kwargs):
            cap = original(*args, **kwargs)
            issued.append(cap)
            return cap

        turn.run.side_effect = run
        with patch.object(self.artifacts, "begin_run", side_effect=begin):
            result = chat.reply("42", "hello", "1", include_details=True)
        self.assertEqual((result["text"], len(result["items"]), result["items"][0]["type"]), ("", 1, "image"))
        self.assertEqual(chat.reply("42", "hello", "1", include_details=True), result)
        with self.assertRaises(PermissionError):
            self.artifacts.scope(issued[0])

    def test_zero_message_empty_final_has_bounded_fallback(self):
        chat, turn = self.chat()
        turn.run.return_value.final_response = ""
        result = chat.reply("42", "hello", "1")
        self.assertEqual(result["reply_status"], "completed")
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["text"])
        self.assertLess(len(result["text"]), 1024)

    def test_caller_timeout_finalizes_while_worker_remains_blocked(self):
        chat, turn = self.chat(timeout=1)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        response = turn.run.return_value
        response.final_response = "LATE INTERNAL SUCCESS"

        def run():
            with self.database.connect() as db:
                self.deliveries.append(db, "42", "1", "progress", [{"type": "text", "text": "Working"}])
            entered.set()
            release.wait(5)
            return response

        turn.run.side_effect = run
        with self.assertRaises(TimeoutError):
            chat.reply("42", "hello", "1", include_details=True)
        self.assertTrue(entered.is_set())
        failed = self.deliveries.get("42", "1")
        self.assertEqual(failed["reply_status"], "failed")
        self.assertEqual(len(failed["items"]), 2)
        self.assertIn("could not finish", failed["items"][-1]["text"])
        self.assertTrue(chat.lock.locked())
        with self.assertRaises(BlockingIOError):
            chat.reply("42", "new", "2")
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT state,capability FROM artifact_runs").fetchone(), ("failed", None))
            self.assertEqual(db.execute("SELECT status FROM reply_runs").fetchone()[0], "failed")
        release.set()
        self.assertTrue(chat.lock.acquire(timeout=3))
        chat.lock.release()
        result = chat.reply("42", "hello", "1", include_details=True)
        self.assertEqual(result["items"], failed["items"])
        self.assertNotIn("LATE INTERNAL", json.dumps(result))
        with self.assertRaises(ValueError):
            chat.reply("42", "changed", "1")
        turn.run.assert_called_once()

    def test_timeout_before_request_row_exists_persists_failed_identity(self):
        chat, turn = self.chat(timeout=0.3)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = chat.codex_factory

        def delayed(*args):
            entered.set()
            release.wait(4)
            return original(*args)

        chat.codex_factory = delayed
        with self.assertRaises(TimeoutError):
            chat.reply("42", "hello", "1")
        self.assertTrue(entered.is_set())
        self.assertEqual(self.deliveries.get("42", "1")["reply_status"], "failed")
        release.set()
        self.assertTrue(chat.lock.acquire(timeout=3))
        chat.lock.release()
        self.assertEqual(chat.reply("42", "hello", "1")["reply_status"], "failed")
        with self.assertRaises(ValueError):
            chat.reply("42", "different", "1")
        turn.run.assert_not_called()

    def test_http_worker_message_retry_survives_source_deletion_and_preserves_order(self):
        from src.worker.message_mcp import MessageClient
        from src.worker.attachment_mcp import open_output, RequestUncertain

        cap = self.artifacts.begin_run("42", "1", 60)
        server = self.server()
        client = MessageClient(cap, "10.203.17.30")
        client.url = f"http://127.0.0.1:{server.server_port}/messaging/worker"
        original_request = client.request
        lost = [True]

        def lost_ack(path, data, headers=None):
            result = original_request(path, data, headers)
            if path == "/send" and lost:
                lost.pop()
                raise RequestUncertain("Synthetic lost acknowledgement")
            return result

        client.request = lost_ack
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "report.txt"
            file.write_bytes(b"result-bytes")
            parts = [{"type": "text", "text": "Before"}, {"type": "file", "path": str(file), "caption": "Report"},
                     {"type": "text", "text": "After"}]
            with patch("src.worker.message_mcp.open_output", side_effect=lambda path: open_output(path, (directory,))):
                with self.assertRaises(RequestUncertain):
                    client.send("answer", parts)
                file.unlink()
                first = client.send("answer", parts)
                self.assertEqual(client.send("answer", parts), first)
                with self.assertRaises(RuntimeError):
                    client.send("answer", [{"type": "text", "text": "Changed"}])
        self.assertEqual(first["state"], "queued")
        snapshot = self.deliveries.snapshot("42", "1")
        self.assertEqual([i["type"] for i in snapshot["items"]], ["text", "file", "text"])
        self.assertIsNone(self.deliveries.get("42", "1"))
        self.assertEqual(self.deliveries.claim(["42"])["item"]["text"], "Before")
        with self.assertRaises(RuntimeError):
            client.request("/send", json.dumps({"message_id": "injected", "request_signature": "a" * 64,
                "items": [{"type": "text", "text": "no"}], "chat_id": "43"}).encode(), {"Content-Type": "application/json"})
        self.assertIsNone(self.deliveries.snapshot("43", "1"))
        self.artifacts.cancel_run("42", "1")
        with self.assertRaises(RuntimeError):
            client.send("later", [{"type": "text", "text": "No late send"}])

    def test_message_batch_rejects_foreign_artifacts_and_rolls_back_late_expiry(self):
        cap = self.artifacts.begin_run("42", "1", 10)
        first = self.upload(capability=cap)
        foreign = self.upload(chat="43", operation="foreign")
        publisher = MessagePublication(self.artifacts, self.deliveries)
        with self.assertRaises(PermissionError):
            publisher.send(cap, "foreign", "a" * 64, [{"type": "file", "artifact_id": first["artifact_id"]},
                                                     {"type": "file", "artifact_id": foreign["artifact_id"]}])
        self.assertEqual(self.artifacts.describe(first["artifact_id"], "42", "1")["state"], "prepared")
        original = self.deliveries.append

        def expired_after_mutation(*args, **kwargs):
            result = original(*args, **kwargs)
            self.now += 11
            return result

        with patch.object(self.deliveries, "append", side_effect=expired_after_mutation), self.assertRaises(PermissionError):
            publisher.send(cap, "late", "b" * 64, [{"type": "file", "artifact_id": first["artifact_id"]}])
        self.assertIsNone(self.deliveries.snapshot("42", "1"))
        self.assertEqual(self.artifacts.describe(first["artifact_id"], "42", "1")["state"], "prepared")

    def test_send_waiting_on_run_or_request_lock_rejects_expired_authority(self):
        for request, kind in (("1", "run"), ("2", "outgoing")):
            cap = self.artifacts.begin_run("42", request, 10)
            publisher = MessagePublication(self.artifacts, self.deliveries)
            errors, started = [], threading.Event()

            def send():
                started.set()
                try:
                    publisher.send(cap, "answer", "d" * 64, [{"type": "text", "text": "Too late"}])
                except Exception as error:
                    errors.append(error)

            with self.database.connect() as held:
                if kind == "run":
                    held.execute("SELECT state FROM artifact_runs WHERE chat_id='42' AND request_id=%s FOR UPDATE", (request,))
                else:
                    self.deliveries._lock(held, "42", request)
                thread = threading.Thread(target=send)
                thread.start()
                self.assertTrue(started.wait(1))
                # Inspect PostgreSQL's actual waiter, rather than trusting a scheduling delay.
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    with self.database.connect() as check:
                        waiting = check.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'").fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.01)
                self.assertTrue(waiting)
                self.now += 11
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], PermissionError)
            self.assertIsNone(self.deliveries.snapshot("42", request))

    def test_capability_creation_does_not_renew_deadline_after_connection_wait(self):
        from contextlib import contextmanager

        connect = self.database.connect
        deadline = self.now + 2

        @contextmanager
        def delayed_connect():
            with connect() as db:
                self.now += 3
                yield db

        with patch.object(self.database, "connect", side_effect=delayed_connect):
            cap = self.artifacts.begin_run("42", "1", 10, deadline=deadline)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT deadline FROM artifact_runs").fetchone()[0], deadline)
        with self.assertRaises(PermissionError):
            self.artifacts.scope(cap)

    def test_artifact_lock_wait_cannot_revive_expired_unlinked_outputs(self):
        from PIL import Image

        self.artifacts.policy = ArtifactPolicy(staging_seconds=10, retention_seconds=1000,
            output_max_bytes=10000, image_max_bytes=10000, capacity_bytes=100000)
        for request, kind in (("1", "file"), ("2", "image"), ("3", "legacy")):
            with self.subTest(kind=kind):
                cap = self.artifacts.begin_run("42", request, 60)
                content = io.BytesIO()
                Image.new("RGB", (2, 2), "blue").save(content, format="PNG")
                data = content.getvalue()
                info = self.artifacts.upload("42", request, "output", "source", "picture.png", "image/png", len(data),
                    hashlib.sha256(data).hexdigest(), io.BytesIO(data), cap)
                part = {"type": "image" if kind == "image" else "file", "artifact_id": info["artifact_id"], "caption": "Output"}
                stable = self.artifacts.upload("42", request, "output", "stable", "first.txt", "text/plain", 5,
                    hashlib.sha256(b"first").hexdigest(), io.BytesIO(b"first"), cap)
                with self.database.connect() as db:
                    db.execute("UPDATE artifacts SET expires=%s WHERE id=%s", (self.now + 100, stable["artifact_id"]))
                parts = [{"type": "file", "artifact_id": stable["artifact_id"]}, part]
                if kind == "legacy":
                    self.artifacts.prepare_batch(cap, "legacy", parts)
                with self.database.connect() as db:
                    self.deliveries.append(db, "42", request, "progress", [{"type": "text", "text": "Earlier update"}])
                before = self.deliveries.snapshot("42", request)
                expires = info["expires_at"]
                errors = []

                def publish(kind=kind, request=request, cap=cap, parts=parts, errors=errors):
                    try:
                        if kind == "legacy":
                            with self.database.connect() as db:
                                self.artifacts.publish(db, "42", request)
                        else:
                            MessagePublication(self.artifacts, self.deliveries).send(cap, "output", "e" * 64, parts)
                    except Exception as error:
                        errors.append(error)

                with self.database.connect() as maintenance:
                    maintenance.execute("SELECT id FROM artifacts WHERE id=%s FOR UPDATE", (info["artifact_id"],))
                    sender = threading.Thread(target=publish)
                    sender.start()
                    self.addCleanup(sender.join, 3)
                    until, waiting = time.monotonic() + 2, 0
                    while time.monotonic() < until:
                        with self.database.connect() as check:
                            waiting = check.execute("""SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                                AND wait_event_type='Lock' AND query LIKE 'SELECT id FROM artifacts WHERE id=ANY%%'""").fetchone()[0]
                        if waiting:
                            break
                        time.sleep(0.01)
                    self.assertTrue(waiting, "Sender must be waiting on the selected artifact row")
                    self.now = expires + 1
                    maintenance.execute("UPDATE artifacts SET state='expired' WHERE id=%s", (info["artifact_id"],))
                    (self.artifacts.root / info["artifact_id"]).unlink()
                sender.join(3)
                self.assertFalse(sender.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], PermissionError)
                self.assertEqual(self.deliveries.snapshot("42", request), before)
                with self.database.connect() as db:
                    self.assertEqual(db.execute("SELECT state,expires FROM artifacts WHERE id=%s", (info["artifact_id"],)).fetchone(),
                                     ("expired", expires))
                self.assertFalse((self.artifacts.root / info["artifact_id"]).exists())
                self.assertEqual(self.artifacts.describe(stable["artifact_id"], "42", request)["state"], "prepared")
                self.assertEqual(self.artifacts.scope(cap), ("42", request))

    def test_final_publication_failure_revokes_capability_and_leaves_no_manifest(self):
        chat, turn = self.chat()
        with patch.object(self.deliveries, "publish", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaises(RuntimeError):
                chat.reply("42", "hello", "1")
        self.assertIsNone(self.deliveries.get("42", "1"))
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT state,capability FROM artifact_runs").fetchone(), ("cancelled", None))
        replay = chat.reply("42", "hello", "1")
        self.assertIn("could not finish", replay["text"])
        turn.run.assert_called_once()

    def test_cancelled_turn_cannot_publish_after_timeout(self):
        # Keep the run budget valid while making only the caller wait expire quickly.
        chat, turn = self.chat()
        release = threading.Event()
        entered = threading.Event()
        response = turn.run.return_value

        def blocked_turn():
            entered.set()
            return release.wait(3) and response

        turn.run.side_effect = blocked_turn

        class InferenceFuture(Future):
            def result(self, timeout=None):
                if not entered.wait(3):
                    raise AssertionError("Synthetic inference did not start.")
                return super().result(timeout=0.01)

        original = self.artifacts.begin_run
        issued = []

        def begin(chat_id, request_id, seconds, **kwargs):
            token = original(chat_id, request_id, 60)
            issued.append(token)
            return token

        with patch.object(self.artifacts, "begin_run", side_effect=begin), patch(
            "src.agent.chat_service.Future", InferenceFuture
        ):
            with self.assertRaises(TimeoutError):
                chat.reply("42", "hello", "1")
            self.assertTrue(issued)
            with self.assertRaises(PermissionError):
                self.artifacts.scope(issued[0])
            release.set()
            until = time.monotonic() + 3
            while chat.lock.locked() and time.monotonic() < until:
                time.sleep(0.01)
        self.assertFalse(chat.lock.locked())
        failure = self.deliveries.get("42", "1")
        self.assertEqual(failure["reply_status"], "failed")
        self.assertTrue(all(item["type"] == "text" for item in failure["items"]))
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT state,capability FROM artifact_runs").fetchone(), ("failed", None))

    def server(self, chat=None):
        handler = make_handler(Mock(), TOKEN, chat=chat, messaging=MessagingAPI(self.deliveries, self.artifacts))
        server = AgentHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_large_unicode_http_reply_replay_and_poll_advance_share_one_model_call(self):
        chat, turn = self.chat()
        text = "界" * 25000
        turn.run.return_value.final_response = text
        server = self.server(chat)
        client = AgentClient(f"http://127.0.0.1:{server.server_port}", TOKEN)
        first = client.chat(42, "hello", "1")
        self.assertEqual(first["text"], text)
        self.assertEqual(first["items"], [{"type": "text", "text": text}])
        self.assertEqual(client.chat(42, "hello", "1"), first)
        update = {"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "hello"}}
        polling = PollingStore(self.database)
        with patch("src.telegram.bot.api", return_value={}):
            process_update(update, {"42"}, client, "synthetic-token", polling)
        self.assertEqual(polling.offset, 2)
        turn.run.assert_called_once()

    def test_http_authentication_scoping_framing_and_streamed_bytes(self):
        server = self.server()
        cap = self.artifacts.begin_run("42", "1", 60)
        body = b"synthetic-file"
        meta = {"operation": "upload", "filename": "report.txt", "mime": "text/plain", "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        def request(path, token, content, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            self.addCleanup(connection.close)
            connection.request("POST", path, content, {"Authorization": "Bearer " + token, **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        self.assertEqual(request("/messaging/worker/upload", "invalid", body)[0], 403)
        code, response = request("/messaging/worker/upload", cap, body, {"X-Artifact-Metadata": json.dumps(meta)})
        self.assertEqual(code, 200)
        self.assertEqual(response["size"], len(body))
        self.assertEqual(request("/messaging/worker/batch", cap, json.dumps({"operation": "batch", "items": [], "chat_id": "43"}).encode())[0], 400)
        self.assertEqual(request("/messaging/claim", cap, b'{}')[0], 403)
        with self.database.connect() as db:
            self.deliveries.append(db, "42", "1", "early", [{"type": "text", "text": "Still working"}])
        client = AgentClient(f"http://127.0.0.1:{server.server_port}", TOKEN)
        self.assertIsNone(client.lookup_reply("42", "1"))
        self.assertEqual(client.snapshot_reply("42", "1")["reply_status"], "pending")
        self.assertEqual(request("/messaging/snapshot", cap, b'{"chat_id":"42","request_id":"1"}')[0], 403)
        self.assertEqual(request("/messaging/worker/upload", cap, body, {"X-Artifact-Metadata": json.dumps(meta), "Transfer-Encoding": "chunked"})[0], 400)


if __name__ == "__main__":
    unittest.main()
