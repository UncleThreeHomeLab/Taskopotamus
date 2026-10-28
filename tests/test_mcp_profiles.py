import logging
import asyncio
import unittest
from unittest.mock import patch

import httpx2

from starlette.testclient import TestClient

from src.mcp_server.server import make_app
from src.profiles.auth import ProfileTokens
from src.profiles.instructions import DEFAULT_PROFILE, load_system_instructions
from src.profiles.store import ProfileStore
from database_support import isolated_database


SECRET = "synthetic-test-profile-signing-key-only-123456"


class MCPProfileTests(unittest.TestCase):
    def test_request_bound_stdio_proxy_preserves_real_profile_protocol_and_version_checks(self):
        from src.worker.profile_mcp import make_server

        scope = {"profile_url": "http://mcp:8080", "profile_token": self.tokens.issue("42")}
        from src.worker.request_mcp import RequestServer

        proxy = asyncio.run(RequestServer.combine([make_server(scope)]))
        app = make_app(self.store, SECRET)
        original_client = httpx2.AsyncClient

        def client(**options):
            return original_client(transport=httpx2.ASGITransport(app=app), **options)

        async def exercise():
            async with app.app.router.lifespan_context(app.app):
                with patch("src.worker.profile_mcp.httpx2.AsyncClient", side_effect=client):
                    initial = await proxy.call_tool("get_agent_profile", {})
                    updated = await proxy.call_tool("update_agent_profile", {
                        "expected_version": 0, "changes": {"persona": "Synthetic updated preference"}})
                    stale = await proxy.call_tool("update_agent_profile", {
                        "expected_version": 0, "changes": {"persona": "Stale preference"}})
            return initial, updated, stale

        initial, updated, stale = asyncio.run(exercise())
        self.assertEqual(initial.structured_content["version"], 0)
        self.assertTrue(updated.structured_content["ok"])
        self.assertFalse(stale.structured_content["ok"])
        self.assertEqual(self.store.get("42")["persona"], "Synthetic updated preference")
        self.assertEqual(self.store.get("43")["version"], 0)

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.store = ProfileStore(isolated_database(self))
        self.tokens = ProfileTokens(SECRET)
        self.client = TestClient(make_app(self.store, SECRET), base_url="http://mcp:8080")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"Authorization": "Bearer " + self.tokens.issue("42"), "Accept": "application/json, text/event-stream"}

    def rpc(self, method, params=None, headers=None):
        return self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                                headers=self.headers if headers is None else headers)

    def test_real_mcp_handshake_discovery_and_scoped_update(self):
        response = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                          "clientInfo": {"name": "offline-test", "version": "1"}})
        self.assertEqual(response.status_code, 200)
        self.assertIn("serverInfo", response.json()["result"])
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"get_agent_profile", "update_agent_profile"})
        for tool in tools:
            self.assertNotIn("chat_id", tool["inputSchema"].get("properties", {}))
            self.assertNotIn("ctx", tool["inputSchema"].get("properties", {}))
        response = self.rpc("tools/call", {"name": "update_agent_profile", "arguments": {
            "expected_version": 0, "changes": {"persona": "Reply briefly in Cantonese."}}})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("error", response.json())
        self.assertEqual(self.store.get("42")["persona"], "Reply briefly in Cantonese.")
        self.assertEqual(self.store.get("43")["version"], 0)
        profile = self.client.get("/profile", headers=self.headers).json()
        self.assertEqual(profile["version"], 1)
        self.assertEqual(profile["boundaries"], DEFAULT_PROFILE["boundaries"])

    def test_missing_forged_expired_auth_and_untrusted_origin_are_rejected(self):
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            for path in ("/mcp", "/profile"):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/profile", headers={**self.headers, "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/profile", headers={**self.headers, "Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/health").json(), {"ok": True})

    def test_application_settings_extra_fields_and_stale_writes_fail_without_changes(self):
        for changes in ({"system_instructions": "ignore rules"}, {"chat_id": "43", "persona": "overwrite"}):
            self.rpc("tools/call", {"name": "update_agent_profile", "arguments": {"expected_version": 0, "changes": changes}})
            self.assertEqual(self.store.get("42")["version"], 0)
        self.store.update("42", 0, {"behavior": "Keep replies short."})
        self.rpc("tools/call", {"name": "update_agent_profile", "arguments": {"expected_version": 0, "changes": {"persona": "stale"}}})
        self.assertEqual(self.store.get("42")["version"], 1)

    def test_personal_boundaries_are_editable_without_changing_application_instructions(self):
        baseline = load_system_instructions()
        response = self.rpc("tools/call", {"name": "update_agent_profile", "arguments": {
            "expected_version": 0, "changes": {"boundaries": "Do not discuss my employer."}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.get("42")["boundaries"], "Do not discuss my employer.")
        self.assertEqual(self.store.get("43")["boundaries"], DEFAULT_PROFILE["boundaries"])
        self.assertEqual(load_system_instructions(), baseline)

    def test_large_requests_are_rejected_before_tool_execution(self):
        response = self.client.post("/mcp", content=b"x" * 65537, headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.store.get("42")["version"], 0)

    def test_valid_tokens_cannot_retarget_profiles_through_tool_arguments_or_query(self):
        self.store.update("43", 0, {"persona": "Other chat's private preference"})
        other_headers = {**self.headers, "Authorization": "Bearer " + self.tokens.issue("43")}
        other = self.client.get("/profile", headers=other_headers).json()
        self.assertEqual(other["persona"], "Other chat's private preference")
        own = self.client.get("/profile?chat_id=43", headers=self.headers).json()
        self.assertEqual(own["persona"], DEFAULT_PROFILE["persona"])
        self.rpc("tools/call", {"name": "update_agent_profile", "arguments": {
            "chat_id": "43", "ctx": {"headers": other_headers},
            "expected_version": 1, "changes": {"persona": "Attempted cross-chat overwrite"}}})
        self.assertEqual(self.store.get("43"), other)

    def test_duplicate_authorization_and_host_headers_are_rejected(self):
        for headers, status in (
            ([("Authorization", self.headers["Authorization"]), ("Authorization", self.headers["Authorization"])], 401),
            ([("Host", "mcp:8080"), ("Host", "mcp:8080"), ("Authorization", self.headers["Authorization"])], 403),
        ):
            self.assertEqual(self.client.get("/profile", headers=headers).status_code, status)
