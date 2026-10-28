"""Offline lifecycle checks: no credentials, model calls, or external services."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from openai_codex import Codex, CodexConfig
from openai_codex.generated.v2_all import ThreadLoadedListResponse

from src.agent.runtime_session import RuntimeSessions, RuntimeLease, RuntimeState, RuntimePaused, RuntimeClosed
from src.agent.worker_transport import WorkspaceStorageError
from src.worker.request_scope import validate_scope, load_scope


def scope(token="a"):
    return {"profile_token": "synthetic-profile", "artifact_token": token * 43, "agent_ip": "10.203.17.30",
            "profile_url": "http://mcp:8080", "deadline": time.time() + 60}


class RuntimeSessionTests(unittest.TestCase):
    def setUp(self):
        self.clients, self.calls = [], []

        def factory():
            client = Mock()
            client._client._proc.poll.return_value = None

            def request(method, params, response_model):
                self.calls.append((method, params))
                if method.endswith("/begin"):
                    return RuntimeState(generation=0, retention_seconds=600, expires_at=time.time() + 600,
                                        sweep_seconds=60, max_bytes=1000, cleaning=False, usage_bytes=0,
                                        admission_blocked=False, runtime_active=True)
                if method.endswith("/end"):
                    return RuntimePaused(paused=True)
                if method.endswith("/close"):
                    return RuntimeClosed(closed=True)
                return ThreadLoadedListResponse(data=[], next_cursor=None)

            client._client.request.side_effect = request
            self.clients.append(client)
            return client

        self.factory = Mock(side_effect=factory)
        self.sessions = RuntimeSessions(self.factory)
        self.addCleanup(self.sessions.shutdown)

    def lease(self, chat="42", config=None, token="a"):
        return self.sessions.lease(chat, config or {"model": "synthetic"}, "synthetic-profile", token * 43, 60)

    def test_same_chat_reuses_native_and_rotates_request_scope(self):
        with self.lease() as first:
            self.assertFalse(first.reused)
        with self.lease(token="b") as second:
            self.assertTrue(second.reused)
            self.assertIs(first.client, second.client)
        self.assertEqual(self.factory.call_count, 1)
        begins = [params for method, params in self.calls if method.endswith("/begin")]
        self.assertNotEqual(begins[0]["scope"]["artifact_token"], begins[1]["scope"]["artifact_token"])

    def test_chat_config_reset_and_auth_invalidate_without_killing_active_work(self):
        with self.lease():
            with self.assertRaises(BlockingIOError), self.sessions.authentication():
                pass
        with self.lease(chat="console:local"):
            pass
        with self.lease(chat="console:local", config={"model": "other"}):
            pass
        with self.sessions.authentication():
            self.assertIsNone(self.sessions.client)
        with self.lease():
            pass
        self.sessions.close()
        self.assertEqual(self.factory.call_count, 4)
        for client in self.clients:
            client.close.assert_called_once()

    def test_failed_turn_closes_without_replaying_then_new_request_can_start(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"), self.lease():
            raise RuntimeError("synthetic failure")
        with self.lease() as lease:
            self.assertFalse(lease.reused)
        self.assertEqual(self.factory.call_count, 2)

    def test_natural_idle_exit_requires_trusted_cleanup_then_restarts(self):
        with self.lease():
            pass
        old = self.clients[0]
        old._client._proc.poll.return_value = 0
        old._client.request.side_effect = RuntimeError("transport closed")
        self.sessions.cleanup_status = Mock(side_effect=[{"runtime_active": True}, {"runtime_active": False}])
        with self.lease() as lease:
            self.assertFalse(lease.reused)
        self.assertFalse(self.sessions.failed)
        self.assertEqual(self.factory.call_count, 2)

    def test_uncertain_cleanup_quarantines_future_requests(self):
        with self.lease():
            pass
        self.clients[0]._client.request.side_effect = RuntimeError("transport closed")
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            self.sessions.close()
        with self.assertRaisesRegex(RuntimeError, "restart"), self.lease():
            pass

    def test_begin_capacity_refusal_preserves_storage_error_after_confirmed_cleanup(self):
        with self.lease():
            pass
        previous = self.clients[0]._client.request.side_effect

        def request(method, params, response_model):
            if method.endswith("/begin"):
                raise RuntimeError("synthetic worker capacity refusal")
            return previous(method, params, response_model)

        self.clients[0]._client.request.side_effect = request
        self.sessions.cleanup_status = Mock(return_value={"runtime_active": False, "admission_blocked": True})
        with self.assertRaises(WorkspaceStorageError), self.lease():
            pass
        self.assertFalse(self.sessions.failed)
        self.assertIsNone(self.sessions.client)
        for stream in (self.clients[0]._client._proc.stdin, self.clients[0]._client._proc.stdout,
                       self.clients[0]._client._proc.stderr):
            stream.close.assert_called_once()

    def test_every_loaded_page_is_archived_and_verified_empty(self):
        client = Mock()
        client._client.request.side_effect = [ThreadLoadedListResponse(data=["parent"], next_cursor="page2"),
                                            ThreadLoadedListResponse(data=["child"], next_cursor=None),
                                            ThreadLoadedListResponse(data=[], next_cursor=None), RuntimePaused(paused=True)]
        lease = RuntimeLease(client, {}, False)
        lease.finish()
        self.assertEqual([call.args[0] for call in client.thread_archive.call_args_list], ["parent", "child"])
        self.assertEqual([call.args[0] for call in client.thread_unarchive.call_args_list], ["parent", "child"])
        self.assertEqual(client._client.request.call_args.args[0], "taskopotamus/runtime/end")

    def test_published_reply_can_discard_runtime_without_retrying_turn(self):
        with self.lease() as lease:
            lease.published = True
            lease.discarded = True
        self.assertIsNone(self.sessions.client)
        self.assertFalse(self.sessions.failed)


class RequestScopeTests(unittest.TestCase):
    def test_malformed_expired_and_wrong_endpoints_fail(self):
        value = scope()
        for updates in ({"deadline": time.time() - 1}, {"deadline": float("nan")}, {"artifact_token": "short"},
                        {"agent_ip": "127.0.0.1"}, {"profile_url": "http://user:secret@mcp:8080"}, {"extra": True}):
            with self.subTest(fields=tuple(updates)), self.assertRaises((ValueError, TypeError)):
                validate_scope({**value, **updates})

    @unittest.skipUnless(getattr(os, "geteuid", lambda: -1)() == 0, "Root-owned snapshot checks require isolated root container")
    def test_snapshot_is_immutable_and_unsafe_files_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scope"
            path.write_text(json.dumps(scope()))
            os.chown(path, 0, 10001)
            path.chmod(0o440)
            first = load_scope(path)
            path.write_text(json.dumps(scope("b")))
            self.assertNotEqual(first["artifact_token"], load_scope(path)["artifact_token"])
            for mode in (0o640, 0o444):
                path.chmod(mode)
                with self.assertRaises(PermissionError):
                    load_scope(path)
            path.chmod(0o440)
            target = Path(directory) / "target"
            path.rename(target)
            path.symlink_to(target)
            with self.assertRaises(OSError):
                load_scope(path)


@unittest.skipIf(os.name == "nt", "Pinned native Linux configuration check")
class NativeArchiveTests(unittest.TestCase):
    def test_archive_unloads_native_tools_and_resume_reads_fresh_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, observed = root / "scope", root / "observed"
            source.write_text("first")
            program = ("from pathlib import Path; import os; from mcp.server import MCPServer; "
                       f"snapshot=Path({str(source)!r}).read_text(); "
                       f"Path({str(observed)!r}).open('a').write(str(os.getpid())+':'+snapshot+'\\n'); "
                       "MCPServer('synthetic').run(transport='stdio')")
            config = {"features.apps": False, "web_search": "disabled", "mcp_servers.synthetic.command": sys.executable,
                      "mcp_servers.synthetic.args": ["-c", program], "mcp_servers.synthetic.required": True}
            with Codex(CodexConfig(env={"CODEX_HOME": directory})) as codex:
                thread = codex.thread_start(cwd=directory, config=config)
                codex.thread_archive(thread.id)
                loaded = codex._client.request("thread/loaded/list", {}, response_model=ThreadLoadedListResponse)
                self.assertEqual(loaded.data, [])
                source.write_text("second")
                codex.thread_unarchive(thread.id)
                self.assertEqual(codex._client.request("thread/loaded/list", {},
                                                     response_model=ThreadLoadedListResponse).data, [])
                codex.thread_resume(thread.id, config=config)
                records = [line.split(":") for line in observed.read_text().splitlines()]
                self.assertEqual([item[1] for item in records], ["first", "second"])
                self.assertNotEqual(records[0][0], records[1][0])
                codex.thread_archive(thread.id)
