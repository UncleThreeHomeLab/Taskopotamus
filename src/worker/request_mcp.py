"""One request-bound MCP process dispatching the existing domain tools unchanged."""
import asyncio
from collections.abc import Mapping

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .calendar_mcp import CalendarClient, make_server as calendar_server
from .gmail_mcp import GmailClient, make_server as gmail_server
from .message_mcp import MessageClient, make_server as message_server
from .profile_mcp import make_server as profile_server
from .request_scope import credentials


class RequestServer(MCPServer):
    def __init__(self, catalog, owners):
        self.catalog, self.owners = catalog, owners
        super().__init__("Taskopotamus Request Tools")

    @classmethod
    async def combine(cls, servers):
        catalog, owners = {}, {}
        for server in servers:
            for tool in await server.list_tools():
                if tool.name in catalog:
                    raise ValueError("Duplicate request tool.")
                catalog[tool.name], owners[tool.name] = tool, server
        return cls(catalog, owners)

    async def list_tools(self):
        return list(self.catalog.values())

    def _tool_input_schema(self, name):
        # MCP 2.3 binds this hook during construction; its empty local tool manager has no domain schemas.
        tool = self.catalog.get(name)
        return tool.input_schema if tool is not None else None

    async def call_tool(self, name, arguments, context=None):
        server = self.owners.get(name)
        if server is None:
            raise ToolError("Unknown request tool.")
        return await server.call_tool(name, arguments, context)


async def make_server(scope):
    token, address = scope["artifact_token"], scope["agent_ip"]
    return await RequestServer.combine([
        message_server(MessageClient(token, address)),
        gmail_server(GmailClient(token, address)),
        calendar_server(CalendarClient(token, address)),
        profile_server(scope),
    ])


async def make_scoped_server(current_scope):
    """Register domain schemas once; bind clients to one admitted call's snapshot."""
    class ScopeView(Mapping):
        def __getitem__(self, key):
            return current_scope()[key]

        def __iter__(self):
            return iter(current_scope())

        def __len__(self):
            return len(current_scope())

    class BoundClient:
        def __init__(self, kind):
            self.kind = kind

        def __getattr__(self, name):
            def call(*arguments, **options):
                scope = current_scope()
                client = self.kind(scope["artifact_token"], scope["agent_ip"])
                return getattr(client, name)(*arguments, **options)
            return call

    return await RequestServer.combine([
        message_server(BoundClient(MessageClient)), gmail_server(BoundClient(GmailClient)),
        calendar_server(BoundClient(CalendarClient)), profile_server(ScopeView()),
    ])


async def main():
    scope = credentials()
    server = await make_server(scope)
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
