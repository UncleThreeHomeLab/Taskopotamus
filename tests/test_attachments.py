import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from database_support import isolated_database
from src.agent.chat_service import ChatService
from src.messaging.artifacts import ArtifactPolicy, ArtifactStore
from src.messaging.delivery_store import DeliveryStore
from src.messaging.messages import delivery_items
from src.telegram.media import TelegramRejected, send_media, rejection
from src.worker.attachment_mcp import AttachmentClient, open_output


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.database = isolated_database(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.store = ArtifactStore(self.database, Path(self.directory.name) / "artifacts", ArtifactPolicy(), self.clock)
        self.cap = self.store.begin_run("42", "1", 60)

    def upload(self, operation, content=b"verified file", mime="text/plain"):
        return self.store.upload("42", "1", "output", operation, "result.txt", mime, len(content),
                                 hashlib.sha256(content).hexdigest(), io.BytesIO(content), self.cap)

    def test_versioned_replacement_clear_and_historical_retry_release_only_deleted_bytes(self):
        first = self.upload("first")
        first_items = [{"type": "file", "artifact_id": first["artifact_id"], "caption": "first"}]
        self.assertEqual(self.store.prepare_batch(self.cap, "batch1", first_items)["version"], 1)
        second = self.upload("second", b"corrected file")
        second_items = [{"type": "file", "artifact_id": second["artifact_id"], "caption": "corrected"}]
        self.store.prepare_batch(self.cap, "batch2", second_items, 1)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT sum(size) FROM artifacts WHERE state!='expired'").fetchone()[0], first["size"] + second["size"])
        with self.assertRaises(PermissionError):
            self.store.describe(first["artifact_id"], "42")
        self.store.maintain()
        self.assertFalse((self.store.root / first["artifact_id"]).exists())
        self.assertEqual(self.upload("first")["state"], "superseded")
        old = self.store.prepare_batch(self.cap, "batch1", first_items)
        self.assertEqual((old["state"], old["current_version"]), ("superseded", 2))
        with self.assertRaises(ValueError):
            self.store.prepare_batch(self.cap, "stale", second_items, 0)
        self.store.prepare_batch(self.cap, "clear", [], 2)
        with self.database.connect() as db:
            self.assertEqual(self.store.publish(db, "42", "1"), [])

    def test_inline_content_is_validated_not_filename_or_declared_mime(self):
        fake = self.upload("fake", b"<html>not an image</html>", "image/png")
        with self.assertRaises(ValueError):
            self.store.prepare_batch(self.cap, "fakebatch", [{"type": "image", "artifact_id": fake["artifact_id"]}])
        image = io.BytesIO()
        Image.new("RGB", (120, 80), "red").save(image, format="PNG")
        valid = self.upload("valid", image.getvalue(), "image/png")
        self.store.prepare_batch(self.cap, "validbatch", [{"type": "image", "artifact_id": valid["artifact_id"]}])
        self.assertEqual(self.store.describe(valid["artifact_id"], "42")["presentation_mime"], "image/png")

    def test_scope_precedes_constructor_and_startup_failure_can_retry_same_request(self):
        codex, thread, turn = Mock(), Mock(), Mock()
        codex.account.return_value = SimpleNamespace(account={"synthetic": True})
        codex.thread_start.return_value = thread
        thread.id = "synthetic-thread"
        thread.turn.return_value = turn
        turn.run.return_value = SimpleNamespace(status=SimpleNamespace(value="completed"), final_response="ready", usage=None)
        tokens = []
        def factory(profile, token):
            self.assertEqual(self.store.scope(token), ("42", "2"))
            tokens.append(token)
            if len(tokens) == 1:
                raise PermissionError("synthetic startup failure")
            return nullcontext(codex)
        chat = ChatService(factory, self.database, artifacts=self.store)
        with self.assertRaises(PermissionError):
            chat.reply("42", "hello", "2")
        with self.assertRaises(PermissionError):
            self.store.scope(tokens[0])
        self.assertEqual(chat.reply("42", "hello", "2")["text"], "ready")
        config = codex.thread_start.call_args.kwargs["config"]
        self.assertEqual(config["mcp_servers.reply_attachments.env_vars"], ["TASKOPOTAMUS_REQUEST_SCOPE", "TASKOPOTAMUS_ARTIFACT_AUTH", "TASKOPOTAMUS_AGENT_IP"])
        self.assertNotIn(tokens[-1], json.dumps(config))
        turn.run.assert_called_once()

    def test_tool_retry_resolves_superseded_batch_after_original_file_is_deleted(self):
        client = AttachmentClient(self.cap, "10.203.17.30")
        def request(path, data, headers=None):
            if path == "/upload":
                meta = json.loads(headers["X-Artifact-Metadata"])
                return self.store.upload("42", "1", "output", meta["operation"], meta["filename"], meta["mime"],
                                         meta["size"], meta["sha256"], data, self.cap)
            value = json.loads(data)
            if path == "/batch-lookup":
                return {"batch": self.store.batch_lookup(self.cap, value["expected_version"], value["request_key"])}
            result = self.store.prepare_batch(self.cap, value["operation"], value["items"], value["expected_version"], value["request_key"])
            self.store.maintain()
            return result
        client.request = request
        root = Path(self.directory.name)
        old, new = root / "old.txt", root / "new.txt"
        old.write_text("old")
        new.write_text("new")
        first = [{"path": str(old), "presentation": "file", "caption": "description"}]
        corrected = [{"path": str(new), "presentation": "file", "caption": "correct"}]
        original_open = open_output
        with patch("src.worker.attachment_mcp.open_output", side_effect=lambda path: original_open(path, (str(root),))):
            self.assertEqual(client.attach(first)["version"], 1)
            self.assertEqual(client.attach(corrected, 1)["version"], 2)
            old.unlink()
            replay = client.attach(first)
            self.assertEqual((replay["state"], replay["current_version"]), ("superseded", 2))
            self.assertNotIn("description", json.dumps(replay))

    def test_per_chat_pacing_and_durable_rate_limit_attempt_history(self):
        outbox = DeliveryStore(self.database, clock=self.clock)
        with self.database.connect() as db:
            outbox.publish(db, "42", "10", [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}])
        first = outbox.claim(["42"])
        args = (first["delivery_id"], first["sequence"], first["attempt"], "rate_limited", {"retry_after": 3})
        outbox.acknowledge(*args)
        self.assertIsNone(outbox.claim(["42"]))
        self.now += 3
        restarted = DeliveryStore(self.database, clock=self.clock)
        retry = restarted.claim(["42"])
        self.assertNotEqual(retry["attempt"], first["attempt"])
        restarted.acknowledge(*args)
        self.assertIsNone(restarted.claim(["42"]))
        restarted.acknowledge(retry["delivery_id"], 0, retry["attempt"], "delivered", {"message_id": 1})
        self.assertIsNone(restarted.claim(["42"]))
        self.now += 1
        self.assertEqual(restarted.claim(["42"])["sequence"], 1)

    def test_retry_exhaustion_is_terminal_and_transport_uncertainty_is_never_requeued(self):
        outbox = DeliveryStore(self.database, clock=self.clock)
        with self.database.connect() as db:
            outbox.publish(db, "42", "10", [{"type": "text", "text": "hello"}])
        for _ in range(4):
            claim = outbox.claim(["42"])
            outbox.acknowledge(claim["delivery_id"], 0, claim["attempt"], "rate_limited", {"retry_after": 1})
            self.now += 1
        self.assertEqual(outbox.status("42")[0]["state"], "failed")
        self.assertIsNone(outbox.claim(["42"]))
        with self.database.connect() as db:
            outbox.publish(db, "42", "11", [{"type": "text", "text": "next"}])
        claim = outbox.claim(["42"])
        outbox.acknowledge(claim["delivery_id"], 0, claim["attempt"], "uncertain")
        self.now += 10
        self.assertIsNone(outbox.claim(["42"]))

    def test_rate_limit_cooldown_covers_other_replies_after_restart_and_exhaustion(self):
        outbox = DeliveryStore(self.database, clock=self.clock)
        for chat, retry_count in (("42", 0), ("43", 3)):
            with self.subTest(retry_count=retry_count):
                with self.database.connect() as db:
                    for request in ("10", "11"):
                        outbox.publish(db, chat, request, [{"type": "text", "text": "queued reply"}])
                    outbox.publish(db, "99", chat, [{"type": "text", "text": "independent chat"}])
                first = outbox.claim([chat])
                with self.database.connect() as db:
                    db.execute("UPDATE outgoing_items SET retry_count=%s WHERE reply_id=%s AND sequence=%s",
                               (retry_count, first["delivery_id"], first["sequence"]))
                starts = self.now
                outbox.acknowledge(first["delivery_id"], first["sequence"], first["attempt"], "rate_limited", {"retry_after": 5})
                self.now = starts + 2
                restarted = DeliveryStore(self.database, clock=self.clock)
                self.assertIsNone(restarted.claim([chat]))
                other = restarted.claim(["99"])
                self.assertIsNotNone(other)
                restarted.acknowledge(other["delivery_id"], other["sequence"], other["attempt"], "delivered", {"message_id": 7})
                self.now = starts + 5
                ready = restarted.claim([chat])
                self.assertIsNotNone(ready)
                restarted.acknowledge(ready["delivery_id"], ready["sequence"], ready["attempt"], "uncertain")


@unittest.skipIf(os.name == "nt", "Worker no-follow file access requires Linux")
class WorkerAttachmentTests(unittest.TestCase):
    def test_mutating_source_prevents_preparing_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "report.txt"
            file.write_text("before")
            client = AttachmentClient("s" * 43, "10.203.17.30")
            calls = []
            def request(path, data, headers=None):
                calls.append(path)
                if path == "/batch-lookup":
                    return {"batch": None}
                self.assertEqual(path, "/upload")
                file.write_text("changed during upload")
                return {"artifact_id": "a" * 32}
            client.request = request
            original = open_output
            with patch("src.worker.attachment_mcp.open_output", side_effect=lambda path: original(path, (directory,))):
                with self.assertRaises(ValueError):
                    client.attach([{"path": str(file), "presentation": "file"}])
            self.assertNotIn("/batch", calls)

    def test_safe_open_rejects_symlink_hidden_hardlink_and_outside_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "report.txt"
            file.write_text("safe")
            with open_output(str(file), (directory,)) as source:
                self.assertEqual(source.read(), b"safe")
            (root / "link").symlink_to(file)
            (root / ".home").mkdir()
            (root / ".home" / "credentials").write_text("synthetic")
            for value in (str(root / "link"), str(root / ".home" / "credentials"), "/home/agent/.codex/auth.json", str(root / "../outside")):
                with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                    open_output(value, (directory,))
            os.link(file, root / "hardlink")
            with self.assertRaises(ValueError):
                open_output(str(file), (directory,))

    def test_real_stdio_mcp_initializes_lists_and_calls_one_batched_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "server.py"
            script.write_text("from src.worker.attachment_mcp import make_server\n"
                              "class Fake:\n def attach(self, files, expected_version):\n  return {'state':'prepared','version':1,'count':len(files)}\n"
                              "make_server(Fake()).run(transport='stdio')\n")
            async def exercise():
                parameters = StdioServerParameters(command=sys.executable, args=[str(script)], env={"PYTHONPATH": str(Path.cwd())})
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.assertEqual([tool.name for tool in listed.tools], ["attach_to_reply"])
                        result = await session.call_tool("attach_to_reply", {"files": [{"path": "/workspace/a.txt", "presentation": "file"}]})
                        self.assertFalse(result.is_error)
                        self.assertIn("prepared", str(result))
            asyncio.run(exercise())

    def test_real_message_stdio_exposes_only_scoped_typed_send(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "server.py"
            script.write_text("from src.worker.message_mcp import make_server\n"
                              "class Fake:\n def send(self, message_id, items):\n  return {'state':'queued','message_id':message_id,'count':len(items)}\n"
                              "make_server(Fake()).run(transport='stdio')\n")

            async def exercise():
                parameters = StdioServerParameters(command=sys.executable, args=[str(script)], env={"PYTHONPATH": str(Path.cwd())})
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.assertEqual([tool.name for tool in listed.tools], ["send_message"])
                        result = await session.call_tool("send_message", {"message_id": "answer", "items": [
                            {"type": "text", "text": "Hello"}, {"type": "image", "path": "/workspace/image.png", "caption": "Image"}]})
                        self.assertFalse(result.is_error)
                        self.assertIn("queued", str(result))
                        denied = await session.call_tool("send_message", {"message_id": "wrong", "items": [
                            {"type": "text", "text": "Hello", "chat_id": "43"}]})
                        self.assertTrue(denied.is_error)

            asyncio.run(exercise())


class MultipartTests(unittest.TestCase):
    def test_multipart_exact_bytes_bounded_reads_and_downloadable_caption(self):
        content = b"synthetic bytes" * 20000
        class Source(io.BytesIO):
            def read(self, size=-1):
                if not 0 <= size <= 65536:
                    raise AssertionError("Unbounded read")
                return super().read(size)
        connection = Mock()
        connection.getresponse.return_value = SimpleNamespace(status=200, read=lambda _: b'{"ok":true,"result":{"message_id":7,"document":{"file_id":"synthetic"}}}')
        info = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "filename": "report.txt"}
        result = send_media("synthetic-token", "42", {"type": "file", "caption": "Description"}, info, Source(content), lambda *_args, **_kwargs: connection)
        self.assertEqual(result["message_id"], 7)
        sent = b"".join(call.args[0] for call in connection.send.call_args_list)
        self.assertIn(content, sent)
        self.assertIn(b'filename="report.txt"', sent)
        self.assertIn(b'false\r\n', sent)
        length = next(call.args[1] for call in connection.putheader.call_args_list if call.args[0] == "Content-Length")
        self.assertEqual(len(sent), int(length))
        connection.close.assert_called_once()

    def test_bad_digest_does_not_finish_multipart_and_only_valid_429_can_retry(self):
        connection = Mock()
        with self.assertRaises(RuntimeError):
            send_media("synthetic", "42", {"type": "file"}, {"size": 1, "sha256": "0" * 64, "filename": "x"}, io.BytesIO(b"x"), lambda *_args, **_kwargs: connection)
        connection.getresponse.assert_not_called()
        self.assertEqual(rejection({"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}).retry_after, 3)
        for value in (True, -1, 0, 301, "3"):
            self.assertIsNone(rejection({"ok": False, "error_code": 429, "parameters": {"retry_after": value}}).retry_after)

    def test_caption_overflow_preserves_non_bmp_text_and_item_order(self):
        caption = "\U0001f99b" * 900 + " description"
        items = delivery_items([{"type": "image", "artifact_id": "a" * 32, "caption": caption}, {"type": "file", "artifact_id": "b" * 32, "caption": "file"}])
        self.assertLessEqual(len(items[0]["caption"].encode("utf-16-le")) // 2, 1024)
        self.assertEqual(items[0]["caption"] + items[1]["text"], caption)
        self.assertEqual(items[2]["type"], "file")


if __name__ == "__main__":
    unittest.main()
