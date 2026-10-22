import socket
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

from src.agent.http_server import AgentHTTPServer
from src.agent.server import make_handler


TOKEN = "synthetic-service-token-for-tests-only"


class HTTPSecurityTests(unittest.TestCase):
    def setUp(self):
        self.auth, self.chat = Mock(), Mock()
        self.auth.status.return_value = {"state": "signed_out"}
        self.handler = make_handler(self.auth, TOKEN, self.chat)
        self.server = AgentHTTPServer(("127.0.0.1", 0), self.handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_all_sensitive_endpoints_reject_missing_and_wrong_credentials(self):
        endpoints = (("GET", "/auth/status"), ("POST", "/auth/login"), ("POST", "/auth/login/retry"),
                     ("POST", "/chat"), ("POST", "/chat/reset"))
        for method, path in endpoints:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                with self.subTest(path=path, headers=headers):
                    # No body is sent: authorization must fail before any body read.
                    request = Request(self.url + path, method=method, headers={**headers, "Content-Length": "100"})
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=1)
                    self.assertEqual(caught.exception.code, 403)
                    caught.exception.close()
        self.auth.status.assert_not_called()
        self.auth.login.assert_not_called()
        self.chat.reply.assert_not_called()
        self.chat.reset.assert_not_called()

    def test_authenticated_status_and_public_health_work(self):
        for path, headers in (("/health", {}), ("/auth/status", {"Authorization": f"Bearer {TOKEN}"})):
            with urlopen(Request(self.url + path, headers=headers), timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_partial_body_is_closed_at_read_deadline(self):
        self.handler.read_timeout = 0.1
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
            request = f"POST /chat HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 100\r\n\r\n{{"
            connection.sendall(request.encode())
            self.assertEqual(connection.recv(1), b"")
        self.chat.reply.assert_not_called()

    def test_server_rejects_connections_when_all_slots_are_used(self):
        self.assertTrue(self.server.slots.acquire(blocking=False))
        acquired = 1
        try:
            while self.server.slots.acquire(blocking=False):
                acquired += 1
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
                self.assertEqual(connection.recv(1), b"")
        finally:
            for _ in range(acquired):
                self.server.slots.release()


if __name__ == "__main__":
    unittest.main()
