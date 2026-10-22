import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.agent.codex_auth import CodexAuth
from src.agent.server import make_handler
from src.telegram.agent_client import AgentClient


class CodexAuthTests(unittest.TestCase):
    def setUp(self):
        self.codex = Mock()
        self.codex.account.return_value = SimpleNamespace(account=None)
        self.auth = CodexAuth(self.codex)

    def test_signed_in_does_not_start_another_login(self):
        self.codex.account.return_value = SimpleNamespace(account=object())
        self.assertEqual(self.auth.login(), {"state": "signed_in"})
        self.assertIsNone(self.auth.pending)

    def test_repeated_requests_reuse_pending_login(self):
        handle = Mock(verification_url="https://auth.openai.com/codex/device", user_code="TEST-0000")
        # Keep this test deterministic; completion handling is tested separately.
        with patch("src.agent.codex_auth.threading.Thread"), patch(
            "src.agent.codex_auth.start_device_login", return_value=handle,
        ) as start:
            first = self.auth.login()
            self.assertEqual(self.auth.login(), first)
        start.assert_called_once()
        self.assertEqual(self.auth.status(), {"state": "pending"})
        self.assertEqual(first["user_code"], "TEST-0000")

    def test_completion_clears_pending_login_and_allows_retry_after_failure(self):
        handle = Mock()
        self.auth.pending = handle
        handle.wait.return_value = False
        self.auth._wait(handle)
        self.assertIsNone(self.auth.pending)
        self.assertEqual(self.auth.status(), {"state": "failed"})
        handle.wait.return_value = True
        self.codex.account.return_value = SimpleNamespace(account=object())
        self.auth.pending = handle
        self.auth._wait(handle)
        self.assertEqual(self.auth.status(), {"state": "signed_in"})

    def test_retry_cancels_old_login_and_ignores_its_late_completion(self):
        old = Mock()
        old.wait.return_value = False
        new = Mock(verification_url="https://auth.openai.com/codex/device", user_code="TEST-1111")
        self.auth.pending = old
        with patch("src.agent.codex_auth.threading.Thread"), patch(
            "src.agent.codex_auth.start_device_login", return_value=new,
        ):
            result = self.auth.login(restart=True)
        old.cancel.assert_called_once()
        self.assertEqual(result["user_code"], "TEST-1111")
        self.auth._wait(old)
        self.assertIs(self.auth.pending, new)
        self.assertEqual(self.auth.status(), {"state": "pending"})

    def test_internal_http_api_and_client_hide_sdk_errors(self):
        token = "synthetic-service-token-for-tests-only"
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.auth, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = AgentClient(f"http://127.0.0.1:{server.server_port}", token)
        self.assertEqual(client.status(), {"state": "signed_out"})
        self.codex.account.return_value = SimpleNamespace(account=object())
        self.assertEqual(client.login(), {"state": "signed_in"})
        self.codex.account.side_effect = RuntimeError("synthetic-secret")
        with self.assertRaisesRegex(RuntimeError, "Agent service unavailable"):
            client.status()
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(f"{client.base_url}/auth/status", headers={"Authorization": f"Bearer {token}"}), timeout=5)
        self.assertNotIn("synthetic-secret", caught.exception.read().decode())
        caught.exception.close()
        with urlopen(f"{client.base_url}/health", timeout=5) as response:
            self.assertEqual(json.load(response), {"ok": True})


if __name__ == "__main__":
    unittest.main()
