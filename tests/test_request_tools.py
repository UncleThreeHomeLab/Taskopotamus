"""Offline aggregate tool compatibility and pinned native process checks."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from openai_codex import Codex, CodexConfig, Sandbox, ApprovalMode
from openai_codex.generated.v2_all import ThreadLoadedListResponse

from src.agent.request_tools import TOOLS, request_config
from src.worker import request_mcp
from src.worker.calendar_mcp import CalendarClient, make_server as calendar_server
from src.worker.gmail_mcp import GmailClient, make_server as gmail_server
from src.worker.message_mcp import MessageClient, make_server as message_server
from src.worker.profile_mcp import make_server as profile_server


def scope(token="a"):
    return {"artifact_token": token * 43, "agent_ip": "10.203.17.30", "profile_token": "synthetic-profile",
            "profile_url": "http://mcp:8080", "deadline": time.time() + 120}


class RequestToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scope = scope()
        self.messages = MessageClient(self.scope["artifact_token"], self.scope["agent_ip"])
        self.gmail = GmailClient(self.scope["artifact_token"], self.scope["agent_ip"])
        self.calendar = CalendarClient(self.scope["artifact_token"], self.scope["agent_ip"])
        self.messages.send = Mock(return_value={"state": "queued", "message_id": "synthetic"})
        self.gmail.execute = Mock(return_value={"messages": []})
        self.calendar.execute = Mock(return_value={"calendars": []})
        self.servers = [message_server(self.messages), gmail_server(self.gmail), calendar_server(self.calendar),
                        profile_server(self.scope)]
        self.server = await request_mcp.RequestServer.combine(self.servers)

    async def test_exact_original_catalog_and_static_configuration(self):
        originals = {tool.name: tool for server in self.servers for tool in await server.list_tools()}
        aggregate = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(set(aggregate), set(TOOLS))
        self.assertEqual(len(aggregate), 13)
        for name, tool in originals.items():
            self.assertEqual(aggregate[name].model_dump(), tool.model_dump())
            self.assertEqual(self.server._tool_input_schema(name), tool.input_schema)
        self.assertIsNone(self.server._tool_input_schema("unknown"))
        config = request_config()
        self.assertTrue(all(key.startswith("mcp_servers.request_tools.") for key in config))
        self.assertEqual(config["mcp_servers.request_tools.env_vars"], ["TASKOPOTAMUS_REQUEST_SCOPE"])
        self.assertEqual(config["mcp_servers.request_tools.enabled_tools"], list(TOOLS))
        self.assertTrue(config["mcp_servers.request_tools.required"])
        self.assertEqual(config["mcp_servers.request_tools.tool_timeout_sec"], 120)
        self.assertNotIn(self.scope["artifact_token"], json.dumps(config))

    async def test_original_dispatch_and_results_without_copying_callbacks(self):
        child = self.servers[1]
        original = child.call_tool
        child.call_tool = AsyncMock(wraps=original)
        arguments = {"query": "synthetic", "limit": 2}
        expected = await original("search_gmail", arguments)
        actual = await self.server.call_tool("search_gmail", arguments)
        child.call_tool.assert_awaited_once_with("search_gmail", arguments, None)
        self.assertEqual(actual.model_dump(), expected.model_dump())
        self.gmail.execute.assert_called_with({"operation": "search_gmail", **arguments})
        await self.server.call_tool("list_calendars", {"offset": 0, "limit": 2})
        self.calendar.execute.assert_called_once_with({"operation": "list_calendars", "offset": 0, "limit": 2})
        await self.server.call_tool("list_calendar_event_colors", {"calendar_id": "synthetic"})
        self.calendar.execute.assert_called_with({"operation": "list_calendar_event_colors", "calendar_id": "synthetic"})
        listing = {"calendar_id": "synthetic", "time_min": "2026-10-28T00:00:00Z", "time_max": "2026-10-29T00:00:00Z"}
        await self.server.call_tool("list_calendar_events", listing)
        self.calendar.execute.assert_called_with({"operation": "list_calendar_events", **listing, "limit": 5})
        await self.server.call_tool("list_calendar_events", {**listing, "query": "牙醫", "page_token": "opaque"})
        self.calendar.execute.assert_called_with({"operation": "list_calendar_events", **listing, "limit": 5,
                                                 "query": "牙醫", "page_token": "opaque"})
        await self.server.call_tool("send_message", {"message_id": "synthetic", "items": [{"type": "text", "text": "hello"}]})
        self.messages.send.assert_called_once()

    async def test_duplicates_unknown_tools_and_original_strict_guards_fail_before_service_calls(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            await request_mcp.RequestServer.combine([self.servers[1], self.servers[1]])
        with self.assertRaises(ToolError):
            await self.server.call_tool("set_google_permissions", {})
        cases = [
            ("search_gmail", {"query": "synthetic", "chat_id": "43"}),
            ("search_gmail", {"query": "synthetic", "limit": True}),
            ("search_gmail", {"query": "synthetic", "limit": "2"}),
            ("search_gmail", {"query": "synthetic", "limit": 6}),
            ("read_gmail", {"message_id": "../private"}),
            ("list_calendars", {"limit": 1, "chat_id": "43"}),
            ("list_calendars", {"limit": True}),
            ("read_calendar_event", {"calendar_id": True, "event_id": "event"}),
            ("create_calendar_event", {"calendar_id": "synthetic", "operation_key": "a" * 16,
                                       "fields": {}, "operation": "override"}),
        ]
        for name, arguments in cases:
            with self.subTest(tool=name, arguments=arguments), self.assertRaises(Exception):
                await self.server.call_tool(name, arguments)
        self.gmail.execute.assert_not_called()
        self.calendar.execute.assert_not_called()
        self.messages.send.assert_not_called()

    async def test_scope_is_snapshotted_once_before_stdio_accepts_requests(self):
        with patch.object(request_mcp, "credentials", side_effect=[self.scope, scope("b")]) as read, \
                patch.object(request_mcp.RequestServer, "run_stdio_async", new_callable=AsyncMock) as serve, \
                patch.object(request_mcp, "GmailClient", wraps=GmailClient) as gmail, \
                patch.object(request_mcp, "CalendarClient", wraps=CalendarClient) as calendar, \
                patch.object(request_mcp, "MessageClient", wraps=MessageClient) as messages:
            await request_mcp.main()
        read.assert_called_once_with()
        serve.assert_awaited_once_with()
        for client in (gmail, calendar, messages):
            client.assert_called_once_with(self.scope["artifact_token"], self.scope["agent_ip"])

    async def test_real_stdio_dispatch_uses_domain_schemas_and_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            program = ("import asyncio; from pathlib import Path; from src.worker import request_mcp; "
                       f"request_mcp.credentials=lambda:{self.scope!r}; "
                       f"request_mcp.GmailClient.execute=lambda self,payload: (Path({str(calls)!r}).open('a').write(payload['operation']+'\\n') and {{'ok':True}}); "
                       "asyncio.run(request_mcp.main())")
            parameters = StdioServerParameters(command=sys.executable, args=["-c", program], cwd=str(Path.cwd()))
            async with stdio_client(parameters) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    self.assertEqual({tool.name for tool in (await session.list_tools()).tools}, set(TOOLS))
                    self.assertFalse((await session.call_tool("search_gmail", {"query": "synthetic"})).is_error)
                    before = calls.read_text()
                    for arguments in ({"query": "x", "limit": True}, {"query": "x", "chat_id": "43"}):
                        self.assertTrue((await session.call_tool("search_gmail", arguments)).is_error)
                    self.assertTrue((await session.call_tool("list_calendars", {"chat_id": "43"})).is_error)
                    self.assertEqual(calls.read_text(), before)
                    self.assertFalse((await session.call_tool("list_calendars", {"limit": 1})).is_error)


@unittest.skipIf(os.name == "nt", "Pinned native process check requires Linux")
class NativeAggregateTests(unittest.TestCase):
    def test_one_aggregate_process_per_thread_and_fresh_scope_after_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, observed = root / "scope", root / "observed"
            source.write_text(json.dumps(scope()))
            program = ("import asyncio,json,os; from pathlib import Path; from taskopotamus_worker import request_mcp\n"
                       "def credentials():\n"
                       f" value=json.loads(Path({str(source)!r}).read_text())\n"
                       f" Path({str(observed)!r}).open('a').write(str(os.getpid())+':'+value['artifact_token']+'\\n')\n"
                       " return value\n"
                       "request_mcp.credentials=credentials\nasyncio.run(request_mcp.main())\n")
            config = request_config()
            config.update({"features.apps": False, "web_search": "disabled",
                           "mcp_servers.request_tools.command": sys.executable,
                           "mcp_servers.request_tools.args": ["-c", program]})
            codex = Codex(CodexConfig(env={"CODEX_HOME": directory}))
            process = codex._client._proc
            try:
                thread = codex.thread_start(cwd=directory, config=config, sandbox=Sandbox.full_access,
                                            approval_mode=ApprovalMode.deny_all)
                first_pid = int(observed.read_text().split(":")[0])
                def native_children():
                    children = set()
                    for task in Path(f"/proc/{process.pid}/task").iterdir():
                        try:
                            children.update(int(pid) for pid in (task / "children").read_text().split())
                        except FileNotFoundError:
                            pass
                    return children

                self.assertEqual(native_children(), {first_pid})
                codex.thread_archive(thread.id)
                self.assertEqual(codex._client.request("thread/loaded/list", {},
                                                     response_model=ThreadLoadedListResponse).data, [])
                source.write_text(json.dumps(scope("b")))
                codex.thread_unarchive(thread.id)
                self.assertEqual(codex._client.request("thread/loaded/list", {},
                                                     response_model=ThreadLoadedListResponse).data, [])
                codex.thread_resume(thread.id, cwd=directory, config=config, sandbox=Sandbox.full_access,
                                    approval_mode=ApprovalMode.deny_all)
                records = [line.split(":") for line in observed.read_text().splitlines()]
                self.assertEqual([item[1] for item in records], ["a" * 43, "b" * 43])
                second_pid = int(records[1][0])
                self.assertNotEqual(first_pid, second_pid)
                self.assertEqual(native_children(), {second_pid})
                self.assertFalse(Path(f"/proc/{first_pid}").exists())
                codex.thread_archive(thread.id)
            finally:
                try:
                    codex.close()
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)
                    for name in ("_reader_thread", "_stderr_thread"):
                        getattr(codex._client, name).join(timeout=.5)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
