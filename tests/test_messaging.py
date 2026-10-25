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

    def chat(self, timeout=180):
        codex, thread, turn = Mock(), Mock(), Mock()
        codex.account.return_value = SimpleNamespace(account={"synthetic": True})
        codex.thread_start.return_value = thread
        thread.id = "synthetic-thread"
        thread.turn.return_value = turn
        turn.run.return_value = SimpleNamespace(status=SimpleNamespace(value="completed"), final_response="done", usage=None)
        chat = ChatService(lambda *_: nullcontext(codex), self.database, turn_timeout=timeout, artifacts=self.artifacts, deliveries=self.deliveries)
        return chat, turn

    def test_final_publication_failure_revokes_capability_and_leaves_no_manifest(self):
        chat, turn = self.chat()
        with patch.object(self.deliveries, "publish", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaises(RuntimeError):
                chat.reply("42", "hello", "1")
        self.assertIsNone(self.deliveries.get("42", "1"))
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT state,capability FROM artifact_runs").fetchone(), ("failed", None))
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

        def begin(chat_id, request_id, seconds):
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
            self.assertEqual(db.execute("SELECT state,capability FROM artifact_runs").fetchone(), ("cancelled", None))

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
        self.assertEqual(request("/messaging/worker/upload", cap, body, {"X-Artifact-Metadata": json.dumps(meta), "Transfer-Encoding": "chunked"})[0], 400)


if __name__ == "__main__":
    unittest.main()
