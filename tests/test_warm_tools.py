"""Offline scope, callback and retained conversation invariants."""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from src.agent.request_tools import request_config
from src.agent.runtime_session import RuntimeLease, RuntimeSessions, RuntimeState, RuntimeBound
from src.worker.request_mcp import make_scoped_server
from src.worker.warm_tools import Admission, AuthenticatedHTTP, call_identity, current_scope


def scope(token="a"):
    return {"profile_token": "synthetic-profile", "artifact_token": token * 43,
            "agent_ip": "10.203.17.30", "profile_url": "http://mcp:8080", "deadline": time.time() + 60}


def context(thread="parent", turn="turn-a"):
    return SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": thread, "turnId": turn}))


def active(gate, token="a", turn="turn-a"):
    gate.update({"operation": "activate", "scope": scope(token)})
    gate.update({"operation": "bind", "thread": "parent"})
    gate.update({"operation": "turn_started", "thread": "parent", "turn": turn})


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_reused_while_calls_keep_exact_credential_snapshot(self):
        gate = Admission()
        server = await make_scoped_server(current_scope)
        catalog = server.catalog
        active(gate)
        observations = []
        with patch("src.worker.gmail_mcp.GmailClient.execute",
                   lambda client, payload: observations.append(client.token) or {"messages": []}):
            async with gate.admit(context()):
                await server.call_tool("search_gmail", {"query": "synthetic"})
                gate.update({"operation": "deactivate"})
                self.assertEqual(current_scope()["artifact_token"], "a" * 43)
                with self.assertRaises(ValueError):
                    gate.update({"operation": "activate", "scope": scope("b")})
                await server.call_tool("search_gmail", {"query": "synthetic"})
            active(gate, "b", "turn-b")
            async with gate.admit(context(turn="turn-b")):
                await server.call_tool("search_gmail", {"query": "synthetic"})
        self.assertIs(server.catalog, catalog)
        self.assertEqual(observations, ["a" * 43, "a" * 43, "b" * 43])

    async def test_pending_old_call_cannot_cross_request_epoch(self):
        gate = Admission()
        active(gate)

        async def old_call():
            async with gate.admit(context(turn="future-turn")):
                self.fail("Old call crossed request boundary")

        pending = asyncio.create_task(old_call())
        await asyncio.sleep(0)
        gate.update({"operation": "deactivate"})
        active(gate, "b", "future-turn")
        with self.assertRaises(ToolError):
            await pending
        async with gate.admit(context(turn="future-turn")):
            self.assertEqual(current_scope()["artifact_token"], "b" * 43)

    async def test_missing_conflicting_expired_and_completed_turns_reject(self):
        gate = Admission()
        active(gate)
        with self.assertRaises(ToolError):
            async with gate.admit(None):
                pass
        mismatch = context()
        mismatch.request_context.meta["x-codex-turn-metadata"] = {"thread_id": "other", "turn_id": "turn-a"}
        with self.assertRaises(ToolError):
            call_identity(mismatch)
        gate.update({"operation": "turn_finished", "thread": "parent", "turn": "turn-a"})
        with self.assertRaises(ToolError):
            async with gate.admit(context()):
                pass
        gate.scope["deadline"] = time.time() - 1
        with self.assertRaises(ToolError):
            async with gate.admit(context()):
                pass
        self.assertEqual(gate.inflight, 0)

    async def test_child_requires_private_registration_and_early_matching_registration_works(self):
        gate = Admission()
        active(gate)
        admitted = []

        async def call():
            async with gate.admit(context("child", "child-turn")):
                admitted.append(current_scope()["artifact_token"])

        pending = asyncio.create_task(call())
        await asyncio.sleep(0)
        self.assertFalse(admitted)
        gate.update({"operation": "child", "thread": "child"})
        gate.update({"operation": "turn_started", "thread": "child", "turn": "child-turn"})
        await pending
        self.assertEqual(admitted, ["a" * 43])

    async def test_child_call_counter_distinguishes_parent_and_resets_per_request(self):
        gate = Admission()
        active(gate)
        async with gate.admit(context()):
            pass
        self.assertEqual((gate.calls, gate.child_calls), (1, 0))
        gate.update({"operation": "child", "thread": "child"})
        gate.update({"operation": "turn_started", "thread": "child", "turn": "child-turn"})
        async with gate.admit(context("child", "child-turn")):
            pass
        self.assertEqual((gate.calls, gate.child_calls), (2, 1))
        gate.update({"operation": "deactivate"})
        active(gate, "b", "turn-b")
        self.assertEqual((gate.calls, gate.child_calls), (0, 0))

    async def test_http_instance_auth_rejects_before_mcp_processing(self):
        seen, sent = [], []

        async def app(*arguments):
            seen.append(arguments)

        async def send(value):
            sent.append(value)

        wrapped = AuthenticatedHTTP(app, "a" * 43)
        await wrapped({"type": "http", "headers": []}, None, send)
        self.assertEqual(sent[0]["status"], 403)
        self.assertEqual(seen, [])
        await wrapped({"type": "http", "headers": [(b"authorization", b"Bearer " + b"a" * 43)]}, None, send)
        self.assertEqual(len(seen), 1)


class RetainedThreadTests(unittest.TestCase):
    def test_same_options_reuse_loaded_thread_but_changes_refresh_settings(self):
        client = Mock()
        client._client.request.return_value = RuntimeBound(bound=True)
        sessions = RuntimeSessions(lambda: client)
        thread = SimpleNamespace(id="parent")
        private = {"url": "http://127.0.0.1:12345/mcp", "token": "a" * 43}
        first = RuntimeLease(client, {}, False, sessions=sessions, tools=private)
        options = {"model": "synthetic", "config": {"limit": 10}, "developer_instructions": "owner-a"}
        first.bind(thread, options, "owner-a")
        second = RuntimeLease(client, {}, True, sessions=sessions, tools=private)
        self.assertIs(second.resume("parent", **options), thread)
        client.thread_resume.assert_not_called()
        self.assertEqual(second.thread_action, "reuse")
        self.assertTrue(second.instructions_current("owner-a"))
        self.assertFalse(second.instructions_current("owner-b"))
        second.resume("parent", **{**options, "config": {"limit": 20}})
        client.thread_resume.assert_called_once()
        self.assertEqual(second.thread_action, "configure")

    def test_private_phase_timings_only_admit_known_bounded_integers(self):
        lease = RuntimeLease(Mock(), {}, True, begin_timings={
            "boundary": 12, "prepare": True, "stage_inputs": -1,
            "write_scope": 1860001, "helper_transition": 0, "private-token": "secret"})
        self.assertEqual(lease.begin_timings, {"runtime_boundary": 12, "runtime_helper_transition": 0})

    def test_transport_metadata_never_enters_workspace_and_replaces_stdio_table(self):
        private = {"url": "http://127.0.0.1:12345/mcp", "token": "a" * 43}
        state = RuntimeState(generation=0, retention_seconds=60, expires_at=time.time() + 60, sweep_seconds=30,
                             max_bytes=1000, cleaning=False, usage_bytes=0, admission_blocked=False,
                             runtime_active=True, tools=private, phase_timings_ms={"boundary": 17})
        self.assertNotIn("phase_timings_ms", state.model_dump())
        self.assertNotIn("phase_timings_ms", repr(state))
        self.assertNotIn("tools", state.model_dump())
        self.assertNotIn(private["token"], repr(state))
        config = request_config(private)
        self.assertEqual(set(config), {"mcp_servers.request_tools"})
        self.assertNotIn("command", config["mcp_servers.request_tools"])
        for value in ({**private, "url": "http://example.com/mcp"}, {**private, "token": "bad"}):
            with self.assertRaises(ValueError):
                request_config(value)


class NativeCorrelationTests(unittest.TestCase):
    def proxy(self):
        from src.worker.runtime_supervisor import RuntimeProxy
        proxy = RuntimeProxy.__new__(RuntimeProxy)
        proxy.phase, proxy.parent = "active", "parent"
        proxy.threads, proxy.turns, proxy.pending_starts = {"parent"}, set(), {}
        proxy.completed_turns = set()
        proxy.tools = Mock()
        proxy.to_native = bytearray()
        return proxy

    def observe(self, proxy, method, **params):
        proxy.observe_native(json.dumps({"method": method, "params": params}).encode())

    def start(self, proxy, identifier="new-turn"):
        proxy.controller_frame(json.dumps({"id": "trusted-rpc", "method": "turn/start",
                                          "params": {"threadId": "parent"}}).encode())
        proxy.observe_native(json.dumps({"id": "trusted-rpc", "result": {"turn": {"id": identifier}}}).encode())

    def test_delayed_parent_events_never_authorize_new_scope(self):
        proxy = self.proxy()
        self.observe(proxy, "turn/started", threadId="parent", turn={"id": "old-turn"})
        self.assertFalse(proxy.turns)
        proxy.controller_frame(json.dumps({"id": "new-rpc", "method": "turn/start",
                                          "params": {"threadId": "parent"}}).encode())
        self.observe(proxy, "turn/started", threadId="parent", turn={"id": "old-turn"})
        self.observe(proxy, "turn/started", threadId="parent", turn={"id": "new-turn"})
        self.assertFalse(proxy.turns)
        proxy.observe_native(json.dumps({"id": "new-rpc", "result": {"turn": {"id": "new-turn"}}}).encode())
        self.assertEqual(proxy.turns, {("parent", "new-turn")})
        self.observe(proxy, "turn/completed", threadId="parent", turn={"id": "new-turn"})
        self.observe(proxy, "turn/started", threadId="parent", turn={"id": "new-turn"})
        self.assertFalse(proxy.turns)

    def test_child_lineage_and_old_parent_activity_cannot_authorize(self):
        proxy = self.proxy()
        self.start(proxy)
        self.observe(proxy, "thread/started", thread={"id": "old-child", "parentThreadId": "parent"})
        self.observe(proxy, "item/started", threadId="parent", turnId="old-turn",
                     item={"type": "subAgentActivity", "kind": "started", "agentThreadId": "old-child"})
        self.observe(proxy, "turn/started", threadId="old-child", turn={"id": "old-child-turn"})
        self.assertEqual(proxy.threads, {"parent"})
        self.observe(proxy, "item/started", threadId="parent", turnId="new-turn",
                     item={"type": "subAgentActivity", "kind": "started", "agentThreadId": "child"})
        self.observe(proxy, "turn/started", threadId="child", turn={"id": "child-turn"})
        self.assertIn(("child", "child-turn"), proxy.turns)
        # A new request binds the same persisted parent but grants no old turns.
        proxy.threads, proxy.turns, proxy.completed_turns = {"parent"}, set(), set()
        proxy.pending_starts.clear()
        self.observe(proxy, "turn/started", threadId="parent", turn={"id": "new-turn"})
        self.observe(proxy, "thread/started", thread={"id": "child", "parentThreadId": "parent"})
        self.observe(proxy, "item/completed", threadId="parent", turnId="new-turn",
                     item={"type": "subAgentActivity", "kind": "interacted", "agentThreadId": "child"})
        self.observe(proxy, "turn/started", threadId="child", turn={"id": "child-turn"})
        self.assertEqual(proxy.threads, {"parent"})
        self.assertFalse(proxy.turns)

    def test_completion_before_start_response_is_not_resurrected(self):
        proxy = self.proxy()
        proxy.pending_starts["trusted-rpc"] = "parent"
        self.observe(proxy, "turn/completed", threadId="parent", turn={"id": "done"})
        proxy.observe_native(json.dumps({"id": "trusted-rpc", "result": {"turn": {"id": "done"}}}).encode())
        self.assertFalse(proxy.turns)
