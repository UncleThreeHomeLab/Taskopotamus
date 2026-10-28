"""Offline OAuth lifecycle tests. Google and Telegram calls are always synthetic."""
import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from cryptography.exceptions import InvalidTag

from src.agent.http_server import AgentHTTPServer
from src.google_integration.config import GoogleConfig
from src.google_integration.oauth import BASE_SCOPES, GoogleOAuth, GoogleTransport, GoogleUnavailable, GrantExpired, TOKEN_URL, IDENTITY_URL, REVOKE_URL
from src.google_integration.server import make_handler
from src.google_integration.store import GoogleStore
from src.telegram.bot import reply_for
from src.telegram.google_client import GoogleClient
from src.telegram.inbox import normalize


SECRET = "synthetic-google-control-secret-only-1234567890"
KEY = "ab" * 32


class SyntheticGoogle:
    def __init__(self):
        self.calls = []
        self.subject, self.email = "synthetic-subject", "synthetic@example.test"
        self.failure = None
        self.on_token = None
        self.refresh_rotation = None

    def request(self, url, fields=None, token=None, timeout=10):
        self.calls.append((url, fields, token))
        if self.failure:
            raise self.failure
        if url == IDENTITY_URL:
            return {"sub": self.subject, "email": self.email, "email_verified": True}
        if url == REVOKE_URL:
            return {}
        if self.on_token:
            self.on_token()
        value = {"access_token": "synthetic-access-token-only", "expires_in": 3600,
                 "token_type": "Bearer", "scope": " ".join(BASE_SCOPES)}
        if fields.get("grant_type") == "authorization_code":
            value["refresh_token"] = "synthetic-refresh-token-only"
        elif self.refresh_rotation:
            value["refresh_token"] = self.refresh_rotation
        return value


class GoogleConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now, self.number = 1000.0, 0
        self.config = GoogleConfig("synthetic-client-id", "synthetic-client-secret", "http://localhost:8090/oauth/google/callback")
        self.store = GoogleStore(self.root / "vault", KEY, clock=lambda: self.now)
        self.transport = SyntheticGoogle()
        self.oauth = GoogleOAuth(self.config, self.store, self.transport)

    def control(self, action, chat="42", confirmation=None):
        self.number += 1
        return self.oauth.control(chat, str(self.number), action, confirmation)

    def start(self, chat="42"):
        result = self.control("connect", chat)
        ticket = parse_qs(urlsplit(result["url"]).query)["ticket"][0]
        url, cookie = self.oauth.start(ticket)
        return parse_qs(urlsplit(url).query)["state"][0], cookie, ticket, url

    def finish(self, chat="42"):
        state, cookie, *_ = self.start(chat)
        self.oauth.callback(state, cookie, "synthetic-code")
        pending = self.store.status(chat)["pending"]
        self.control("confirm", chat, pending["confirmation"])

    def test_connection_requires_browser_and_telegram_confirmation_and_all_access_stays_off(self):
        self.assertEqual(self.store.status("42")["state"], "disconnected")
        state, cookie, _, url = self.start()
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(set(query["scope"][0].split()), set(BASE_SCOPES))
        self.assertNotIn("client_secret", query)
        self.oauth.callback(state, cookie, "synthetic-code")
        status = self.store.status("42")
        self.assertEqual(status["state"], "disconnected")
        self.assertEqual(status["pending"]["email"], "synthetic@example.test")
        with self.assertRaises(PermissionError):
            self.oauth.credentials("42")
        confirmation = status["pending"]["confirmation"]
        with self.assertRaises(ValueError):
            self.control("confirm", "43", confirmation)
        self.control("confirm", confirmation=confirmation)
        self.assertEqual(self.store.status("42"), {"state": "active", "email": "synthetic@example.test", "gmail": "off", "calendars": "off"})
        _, credentials = self.oauth.credentials("42")
        self.assertEqual(credentials["permissions"], {"gmail": "off", "calendars": {}})
        with self.assertRaises(PermissionError):
            self.oauth.credentials("43")

    def test_start_state_cookie_replay_expiry_and_denial_fail_closed(self):
        state, cookie, ticket, _ = self.start()
        with self.assertRaises(ValueError):
            self.oauth.start(ticket)
        with self.assertRaises(ValueError):
            self.oauth.callback(state, "wrong-cookie", "synthetic-code")
        self.assertEqual(len(self.transport.calls), 0)
        self.oauth.callback(state, cookie, denied=True)
        with self.assertRaises(ValueError):
            self.oauth.callback(state, cookie, "synthetic-code")
        state, cookie, *_ = self.start()
        self.now += 601
        with self.assertRaises(ValueError):
            self.oauth.callback(state, cookie, "synthetic-code")
        self.assertEqual(len(self.transport.calls), 0)

    def test_confirmation_expires_and_account_switch_requires_disconnect(self):
        self.finish()
        self.transport.subject = "another-subject"
        state, cookie, *_ = self.start()
        with self.assertRaises(ValueError):
            self.oauth.callback(state, cookie, "synthetic-code")
        self.assertNotIn("pending", self.store.status("42"))
        self.control("disconnect")
        state, cookie, *_ = self.start()
        self.oauth.callback(state, cookie, "synthetic-code")
        confirmation = self.store.status("42")["pending"]["confirmation"]
        self.now += 601
        with self.assertRaises(ValueError):
            self.control("confirm", confirmation=confirmation)

    def test_new_connect_and_disconnect_invalidate_claimed_callback_generation(self):
        state, cookie, *_ = self.start()
        self.transport.on_token = lambda: self.control("connect")
        with self.assertRaises(ValueError):
            self.oauth.callback(state, cookie, "synthetic-code")
        self.assertNotIn("pending", self.store.status("42"))
        self.transport.on_token = None
        state, cookie, *_ = self.start()
        self.control("disconnect")
        calls = len(self.transport.calls)
        with self.assertRaises(ValueError):
            self.oauth.callback(state, cookie, "synthetic-code")
        self.assertEqual(len(self.transport.calls), calls)

    def test_control_idempotency_prevents_replayed_disconnect_affecting_new_connection(self):
        self.finish()
        result = self.control("disconnect")
        disconnect_id = str(self.number)
        self.assertTrue(result["revoked"])
        self.finish()
        calls = len(self.transport.calls)
        self.assertEqual(self.oauth.control("42", disconnect_id, "disconnect"), result)
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(self.store.status("42")["state"], "active")
        with self.assertRaises(ValueError):
            self.oauth.control("42", disconnect_id, "connect")
        self.now += 86401
        with self.assertRaises(ValueError):
            self.oauth.control("42", disconnect_id, "disconnect")
        self.assertEqual(self.store.status("42")["state"], "active")

    def test_vault_encrypts_tokens_flows_and_command_responses_with_context_binding(self):
        state, cookie, ticket, _ = self.start()
        self.oauth.callback(state, cookie, "synthetic-code")
        pending = self.store.status("42")["pending"]["confirmation"]
        contents = self.store.path.read_bytes()
        for sensitive in (ticket, cookie, state, pending, "synthetic-access-token-only", "synthetic-refresh-token-only", "synthetic@example.test"):
            self.assertNotIn(sensitive.encode(), contents)
        sealed = self.store.seal("42", 1, "active", {"secret": "synthetic-only"})
        for chat, generation, purpose in (("43", 1, "active"), ("42", 2, "active"), ("42", 1, "pending")):
            with self.assertRaises(InvalidTag):
                self.store.open(chat, generation, purpose, sealed)
        with self.assertRaises(InvalidTag):
            self.store.open("42", 1, "active", sealed[:-1] + bytes([sealed[-1] ^ 1]))
        other = GoogleStore(self.root / "other", "cd" * 32)
        with self.assertRaises(InvalidTag):
            other.open("42", 1, "active", sealed)
        self.assertNotEqual(sealed, self.store.seal("42", 1, "active", {"secret": "synthetic-only"}))

    def test_refresh_rotation_omission_revocation_and_stale_refresh_fail_closed(self):
        self.finish()
        self.now += 3600
        self.transport.refresh_rotation = "synthetic-rotated-refresh-only"
        _, active = self.oauth.credentials("42")
        self.assertEqual(active["refresh_token"], "synthetic-rotated-refresh-only")
        self.transport.refresh_rotation = None
        self.now += 3600
        _, active = self.oauth.credentials("42")
        self.assertEqual(active["refresh_token"], "synthetic-rotated-refresh-only")
        self.now += 3600
        self.transport.on_token = lambda: self.control("disconnect")
        with self.assertRaises(PermissionError):
            self.oauth.credentials("42")
        self.assertEqual(self.store.status("42")["state"], "disconnected")
        self.transport.on_token = None
        self.finish()
        self.now += 3600
        self.transport.failure = GrantExpired("synthetic-expired")
        with self.assertRaises(GrantExpired):
            self.oauth.credentials("42")
        self.assertEqual(self.store.status("42")["state"], "reconnect")

    def test_revocation_failure_still_removes_local_credentials(self):
        self.finish()
        self.transport.failure = GoogleUnavailable("synthetic-provider-error-containing-token")
        self.assertFalse(self.control("disconnect")["revoked"])
        self.assertEqual(self.store.status("42")["state"], "disconnected")
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            self.assertIsNone(row["active"])
            self.assertIsNone(row["pending"])

    def test_disconnect_without_stored_token_never_claims_google_revocation(self):
        self.assertIsNone(self.control("disconnect")["revoked"])
        self.assertEqual(self.transport.calls, [])
        state, cookie, *_ = self.start()
        self.oauth.callback(state, cookie, "synthetic-code")
        self.now += 601
        self.assertIsNone(self.control("disconnect")["revoked"])
        self.assertFalse(any(url == REVOKE_URL for url, _, _ in self.transport.calls))

    def test_stale_invalid_grant_cannot_remove_a_newer_login_flow(self):
        self.finish()
        self.now += 3600
        original = self.transport.request
        new_flow = []

        def stale_refresh(url, fields=None, token=None, timeout=10):
            if url == TOKEN_URL and fields.get("grant_type") == "refresh_token":
                new_flow.append(self.start())
                raise GrantExpired("synthetic-expired")
            return original(url, fields, token)

        self.transport.request = stale_refresh
        with self.assertRaises(GrantExpired):
            self.oauth.credentials("42")
        state, cookie, *_ = new_flow[0]
        self.transport.request = original
        self.oauth.callback(state, cookie, "synthetic-code")
        self.assertIn("pending", self.store.status("42"))

    def test_stale_refresh_cannot_overwrite_a_confirmed_reauthorization(self):
        self.finish()
        state, cookie, *_ = self.start()
        self.oauth.callback(state, cookie, "synthetic-code")
        confirmation = self.store.status("42")["pending"]["confirmation"]
        self.now += 100
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            active = self.store.open("42", row["generation"], "active", row["active"])
            active["expires_at"] = self.now
            db.execute("UPDATE connections SET active=? WHERE chat='42'",
                       (self.store.seal("42", row["generation"], "active", active),))
        self.transport.on_token = lambda: self.control("confirm", confirmation=confirmation)
        with self.assertRaises(PermissionError):
            self.oauth.credentials("42")
        self.assertEqual(self.store.status("42")["state"], "active")

    @contextlib.contextmanager
    def server(self, public=False):
        server = AgentHTTPServer(("127.0.0.1", 0), make_handler(self.oauth, SECRET, public=public))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def request(self, port, path, body=None, headers=None, public=False):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        defaults = {"Host": "localhost:8090" if public else "google:8081"}
        if body is not None:
            body = json.dumps(body)
            defaults["Content-Type"] = "application/json"
        connection.request("GET" if body is None else "POST", path, body=body, headers={**defaults, **(headers or {})})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def test_synthetic_http_flow_control_auth_callback_headers_and_duplicate_parameters(self):
        with self.server() as control, self.server(public=True) as public:
            body = {"chat_id": "42", "request_id": "1", "action": "connect"}
            status, _, _ = self.request(control, "/control", body)
            self.assertEqual(status, 403)
            auth = {"Authorization": "Bearer " + SECRET}
            self.assertEqual(self.request(control, "/control", body, {**auth, "Origin": "https://evil.example"})[0], 403)
            status, _, value = self.request(control, "/control", body, auth)
            self.assertEqual(status, 200)
            start = urlsplit(json.loads(value)["url"])
            status, headers, _ = self.request(public, start.path + "?" + start.query, public=True)
            self.assertEqual(status, 303)
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            state = parse_qs(urlsplit(headers["Location"]).query)["state"][0]
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            path = "/oauth/google/callback?state=" + state + "&code=synthetic-code"
            self.assertEqual(self.request(public, path + "&state=duplicate", headers={"Cookie": cookie}, public=True)[0], 400)
            self.assertEqual(self.request(public, path, public=True)[0], 400)
            status, headers, text = self.request(public, path, headers={"Cookie": cookie}, public=True)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("synthetic-access", text)
            self.assertNotIn("synthetic@example", text)
            self.assertEqual(self.request(public, "/control", body, auth, public=True)[0], 404)
            self.assertEqual(self.request(public, path, headers={"Cookie": cookie}, public=True)[0], 400)
            body = {"chat_id": "42", "request_id": "2", "action": "confirm",
                    "confirmation": self.store.status("42")["pending"]["confirmation"]}
            self.assertEqual(self.request(control, "/control", body, auth)[0], 200)

    def test_google_transport_uses_fixed_endpoints_and_sanitizes_errors(self):
        transport = GoogleTransport()
        with self.assertRaises(ValueError):
            transport.request("https://evil.example/token")
        for code, payload, expected in ((400, {"error": "invalid_grant", "detail": "synthetic-secret"}, GrantExpired),
                                        (500, {"error": "synthetic-secret"}, GoogleUnavailable)):
            response = Mock()
            response.read1.side_effect = [json.dumps(payload).encode(), b""]
            error = HTTPError(TOKEN_URL, code, "synthetic-secret", {}, response)
            with patch.object(transport.opener, "open", side_effect=error), self.assertRaises(expected) as caught:
                transport.request(TOKEN_URL, {"refresh_token": "synthetic-secret"})
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_config_is_optional_validated_and_supports_private_files(self):
        self.assertFalse(GoogleConfig.from_env({}).enabled)
        with self.assertRaises(ValueError):
            GoogleConfig.from_env({"GOOGLE_OAUTH_CLIENT_ID": "synthetic"})
        environment = {"GOOGLE_OAUTH_CLIENT_ID": "synthetic-id", "GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-secret",
                       "GOOGLE_OAUTH_REDIRECT_URI": self.config.redirect_uri}
        path = self.root / "synthetic-secret"
        path.write_text("synthetic-file-secret")
        environment["GOOGLE_OAUTH_CLIENT_SECRET_FILE"] = str(path)
        self.assertEqual(GoogleConfig.from_env(environment).client_secret, "synthetic-file-secret")
        self.assertNotIn("synthetic-file-secret", repr(GoogleConfig.from_env(environment)))
        for uri in ("http://evil.example/oauth/google/callback", "https://user:password@example.test/oauth/google/callback",
                    "https://example.test/other", "https://example.test/oauth/google/callback?secret=x"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                GoogleConfig.from_env({**environment, "GOOGLE_OAUTH_REDIRECT_URI": uri})

    def test_private_telegram_controls_do_not_reach_model_and_forged_binding_is_rejected_before_persistence(self):
        message = {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/google connect"}
        client, agent = Mock(), Mock()
        client.command.return_value = "synthetic-login-result"
        self.assertEqual(reply_for(message, {"42"}, agent, "1", _google=client), "synthetic-login-result")
        client.command.assert_called_once_with(42, "1", "/google connect")
        agent.chat.assert_not_called()
        for rejected in ({**message, "chat": {"id": 43, "type": "private"}},
                         {**message, "chat": {"id": -42, "type": "group"}},
                         {**message, "from": {"id": 43}}):
            self.assertIsNone(normalize({"update_id": 1, "message": rejected}, {"42"}))
            self.assertIsNone(reply_for(rejected, {"42"}, agent, "2", _google=client))
        client.command.side_effect = RuntimeError("synthetic-secret")
        self.assertNotIn("synthetic-secret", reply_for(message, {"42"}, agent, "3", _google=client))

    def test_telegram_client_sends_only_scoped_control_request(self):
        client = GoogleClient("http://google:8081", SECRET)
        response = io.BytesIO(json.dumps({"state": "disconnected"}).encode())
        with patch.object(client.opener, "open", return_value=response) as send:
            self.assertIn("not connected", client.command(42, "1", "/google"))
        request = send.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"chat_id": "42", "request_id": "1", "action": "menu_google"})
        self.assertEqual(request.full_url, "http://google:8081/control")


if __name__ == "__main__":
    unittest.main()
