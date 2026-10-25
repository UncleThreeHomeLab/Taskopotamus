import http.client
import json
import socket
import threading
import time
import io
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

from src.agent.http_server import AgentHTTPServer
from src.agent.run_details import summarize_run
from src.agent.subagents import SubagentPolicy
from src.telegram.agent_client import AgentClient, AgentRequestError
from src.test_console.server import ConsoleState, make_handler


def finished(state):
    until = time.monotonic() + 3
    while state.snapshot()["pending"] and time.monotonic() < until:
        time.sleep(0.01)
    if state.snapshot()["pending"]:
        raise AssertionError("Synthetic console job did not finish")


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.status.return_value = {"state": "signed_in"}
        self.client.console_chat.return_value = {"text": "Hello <script>private</script>", "status": "completed", "details": None}
        self.state = ConsoleState(self.client)
        self.handler = make_handler(self.state)
        self.server = AgentHTTPServer(("127.0.0.1", 0), self.handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method="GET", path="/", fields=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        default = {"Host": "localhost:8088"}
        if method == "POST":
            default.update({"Origin": "http://localhost:8088", "Cookie": f"console_session={self.state.cookie}",
                            "Content-Type": "application/x-www-form-urlencoded"})
        default.update(headers or {})
        connection.request(method, path, urlencode(fields).encode() if fields is not None else None, default)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()

    def fields(self, text=None):
        fields = {"csrf": self.state.csrf, "nonce": self.state.nonce}
        if text is not None:
            fields["text"] = text
        return fields

    def test_page_is_safe_and_loading_does_not_start_live_work(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("Message", body)
        self.client.assert_not_called()
        self.client.status.assert_not_called()
        self.client.console_chat.assert_not_called()

    def test_attachment_preview_download_requires_session_and_streams_exact_bytes(self):
        identifier = "a" * 32
        content = b"synthetic attachment bytes"
        self.client.console_chat.return_value = {"text": "ready", "status": "completed", "items": [
            {"type": "image", "artifact_id": identifier, "caption": "Caption <script>private</script>"}],
            "artifacts": {identifier: {"state": "committed", "filename": "chart.png", "presentation_mime": "image/png", "size": len(content)}}}
        self.client.artifact_stream.side_effect = lambda *_: io.BytesIO(content)
        self.state.submit("chat", self.state.nonce, "make a file")
        finished(self.state)
        body = self.request()[2]
        self.assertIn(f'src="/artifact/{identifier}"', body)
        self.assertIn("Caption &lt;script&gt;private&lt;/script&gt;", body)
        self.assertIn(f'href="/artifact/{identifier}/download"', body)
        self.assertEqual(self.request(path=f"/artifact/{identifier}")[0], 403)
        cookie = {"Cookie": f"console_session={self.state.cookie}"}
        status, headers, data = self.request(path=f"/artifact/{identifier}", headers=cookie)
        self.assertEqual((status, headers["Content-Type"], data.encode()), (200, "image/png", content))
        status, headers, _ = self.request(path=f"/artifact/{identifier}/download", headers=cookie)
        self.assertEqual(status, 200)
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertEqual(self.request(path=f"/artifact/{identifier}", headers={**cookie, "Origin": "https://evil.example"})[0], 403)
        self.client.artifact_stream.side_effect = RuntimeError("expired")
        self.assertEqual(self.request(path=f"/artifact/{identifier}", headers=cookie)[0], 410)

    def test_host_origin_csrf_cookie_and_identity_injection_fail_before_backend(self):
        cases = [({"Host": "evil.example:8088"}, self.fields("hi")),
                 ({"Origin": "https://evil.example"}, self.fields("hi")),
                 ({"Origin": "null"}, self.fields("hi")),
                 ({"Cookie": "console_session=forged"}, self.fields("hi")),
                 ({}, {**self.fields("hi"), "csrf": "bad"}),
                 ({}, {**self.fields("hi"), "chat_id": "42"})]
        for headers, fields in cases:
            with self.subTest(headers=list(headers), fields=list(fields)):
                self.assertIn(self.request("POST", "/chat", fields, headers)[0], (400, 403))
        self.client.console_chat.assert_not_called()
        self.assertEqual(self.request(headers={"Origin": "null"})[0], 403)

    def test_duplicate_send_and_refresh_run_one_turn_and_escape_output(self):
        fields = self.fields("<script>input</script>")
        self.assertEqual(self.request("POST", "/chat", fields)[0], 303)
        finished(self.state)
        self.assertEqual(self.request("POST", "/chat", fields)[0], 303)
        self.client.console_chat.assert_called_once_with("<script>input</script>", fields["nonce"])
        body = self.request()[2]
        self.assertIn("&lt;script&gt;private&lt;/script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("Reply completed", body)

    def test_busy_reset_cannot_clear_transcript_and_pending_page_refreshes(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.client.console_chat.side_effect = lambda *_: release.wait(2) or {}
        self.state.transcript = [("You", "preserved")]
        self.state.submit("chat", self.state.nonce, "hello")
        self.assertIn('http-equiv="refresh"', self.request()[2])
        self.assertEqual(self.request("POST", "/reset", self.fields())[0], 400)
        self.assertEqual(self.state.transcript, [("You", "preserved")])
        self.client.console_reset.assert_not_called()
        self.assertEqual(self.request("POST", "/status", self.fields())[0], 303)
        self.client.status.assert_called_once()
        release.set()
        finished(self.state)

    def test_uncertain_replay_uses_same_request_and_failed_reply_is_not_success(self):
        self.client.console_chat.side_effect = [AgentRequestError("sensitive detail", 504),
                                               {"text": "stored failure", "status": "failed", "details": None}]
        original = self.state.nonce
        self.state.submit("chat", original, "hello")
        finished(self.state)
        self.assertIn("uncertain", self.state.notice)
        self.assertNotIn("sensitive", self.state.notice)
        self.state.submit("replay", self.state.nonce)
        finished(self.state)
        self.assertEqual([call.args for call in self.client.console_chat.call_args_list], [("hello", original), ("hello", original)])
        self.assertEqual(self.state.transcript, [])
        self.assertIn("could not finish", self.state.notice)

    def test_storage_refusal_is_clear_and_never_marked_uncertain(self):
        self.client.console_chat.side_effect = AgentRequestError("synthetic-private-detail", 507)
        self.state.submit("chat", self.state.nonce, "hello")
        finished(self.state)
        self.assertIsNone(self.state.uncertain)
        self.assertEqual(self.state.transcript, [])
        self.assertIn("Workspace storage limit reached", self.state.notice)
        self.assertIn("No work started", self.state.notice)
        self.assertNotIn("synthetic-private", self.state.notice)

    def test_auth_link_validation_and_reset_only_on_success(self):
        self.client.login.return_value = {"state": "pending", "verification_url": "https://evil.example", "user_code": "TEST-0000"}
        self.state.submit("login", self.state.nonce)
        finished(self.state)
        self.assertNotIn("evil.example", self.request()[2])
        self.assertNotIn("TEST-0000", self.request()[2])
        self.client.login.return_value = {"state": "pending", "verification_url": "https://auth.openai.com/codex/device", "user_code": "TEST-0000"}
        self.state.submit("retry", self.state.nonce)
        finished(self.state)
        self.assertIn("TEST-0000", self.request()[2])
        self.client.login.assert_called_with(restart=True)
        self.state.transcript = [("You", "preserved")]
        self.client.console_reset.side_effect = AgentRequestError("synthetic-secret", 429)
        self.state.submit("reset", self.state.nonce)
        finished(self.state)
        self.assertEqual(self.state.transcript, [("You", "preserved")])
        self.client.console_reset.side_effect = None
        self.client.console_reset.return_value = {"text": "New conversation started"}
        self.state.submit("reset", self.state.nonce)
        finished(self.state)
        self.assertEqual(self.state.transcript, [])

    def test_slow_body_is_closed_before_backend_and_oversized_forms_fail(self):
        self.handler.read_timeout = 0.1
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
            connection.sendall(("POST /chat HTTP/1.1\r\nHost: localhost:8088\r\nOrigin: http://localhost:8088\r\n"
                                f"Cookie: console_session={self.state.cookie}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                                "Content-Length: 100\r\n\r\na").encode())
            self.assertEqual(connection.recv(1), b"")
        self.assertEqual(self.request("POST", "/chat", self.fields("x" * 65537))[0], 400)
        self.client.console_chat.assert_not_called()


class RunDetailsTests(unittest.TestCase):
    def test_evidence_is_bounded_allowlisted_and_never_infers_test_success(self):
        command = SimpleNamespace(type="commandExecution", status="completed", exit_code=0, duration_ms=2,
                                  command="synthetic-secret", aggregated_output="10 tests passed synthetic-secret")
        child = SimpleNamespace(type="collabAgentToolCall", status="completed", tool="spawnAgent", model="synthetic-child",
                                prompt="synthetic-secret", agents_states={"private-thread": SimpleNamespace(status="completed", message="secret")})
        result = SimpleNamespace(status="completed", duration_ms=5, final_response="All tests passed", items=[command, child] * 30)
        details = summarize_run(result, "synthetic-parent", "medium", SubagentPolicy())
        self.assertEqual(details["commands"][0]["exit_code"], 0)
        self.assertEqual(details["children"][0]["requested_model"], "synthetic-child")
        self.assertEqual(len(details["commands"]), 20)
        self.assertIsNone(details["actual_execution_model"])
        self.assertIsNone(details["verified_test_result"])
        self.assertNotIn("synthetic-secret", json.dumps(details))
        self.assertNotIn("private-thread", json.dumps(details))

    def test_missing_runtime_details_are_unavailable(self):
        details = summarize_run(SimpleNamespace(status="completed"), "synthetic", "medium", SubagentPolicy())
        self.assertIsNone(details["duration_ms"])
        self.assertEqual(details["children"], [])

    def test_agent_client_refuses_redirects_without_sending_bearer_to_target(self):
        paths = []

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/destination")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = AgentClient(f"http://127.0.0.1:{server.server_port}", "synthetic-long-service-token-only-123456")
        with self.assertRaises(AgentRequestError) as caught:
            client.status()
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(paths, ["/auth/status"])
