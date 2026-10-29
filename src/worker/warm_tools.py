"""One fixed, unprivileged MCP dispatcher with private request admission control."""
import asyncio
import ctypes
import hmac
import json
import os
import secrets
import socket
import sys
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

from mcp.server.mcpserver.exceptions import ToolError

from .request_mcp import make_scoped_server
from .request_scope import validate_scope


MAX_CONTROL = 16384
CALL_SCOPE = ContextVar("taskopotamus_tool_scope")


def current_scope():
    return CALL_SCOPE.get()


def call_identity(context):
    try:
        metadata = context.request_context.meta
        metadata = metadata.model_dump(by_alias=True) if hasattr(metadata, "model_dump") else metadata
        if not isinstance(metadata, dict):
            raise ValueError()
        nested = metadata.get("x-codex-turn-metadata", {})
        if isinstance(nested, str):
            if len(nested) > 8192:
                raise ValueError()
            nested = json.loads(nested)
        if not isinstance(nested, dict):
            raise ValueError()
        identifiers = []
        for flat, inner in (("threadId", "thread_id"), ("turnId", "turn_id")):
            direct, embedded = metadata.get(flat), nested.get(inner)
            if direct is not None and embedded is not None and direct != embedded:
                raise ValueError()
            value = direct if direct is not None else embedded
            if not isinstance(value, str) or not 0 < len(value) <= 128:
                raise ValueError()
            identifiers.append(value)
        return tuple(identifiers)
    except (AttributeError, TypeError, ValueError):
        raise ToolError("Current request and turn metadata are required.") from None


class Admission:
    def __init__(self):
        self.epoch, self.scope, self.parent = 0, None, None
        self.threads, self.turns = set(), set()
        self.inflight = 0
        self.changed = asyncio.Event()
        self.calls, self.child_calls, self.rejected = 0, 0, 0

    def update(self, message):
        operation = message.get("operation")
        if operation == "activate" and set(message) == {"operation", "scope"}:
            if self.scope is not None or self.inflight:
                raise ValueError("Tool request boundary is not quiescent.")
            self.epoch += 1
            self.scope = validate_scope(message["scope"])
            self.parent = None
            self.threads.clear()
            self.turns.clear()
            self.calls, self.child_calls, self.rejected = 0, 0, 0
        elif operation == "bind" and set(message) == {"operation", "thread"}:
            value = message["thread"]
            if self.scope is None or not isinstance(value, str) or not 0 < len(value) <= 128 or self.parent:
                raise ValueError("Invalid tool conversation binding.")
            self.parent = value
            self.threads.add(value)
        elif operation == "child" and set(message) == {"operation", "thread"}:
            value = message["thread"]
            if not self.parent or not isinstance(value, str) or not 0 < len(value) <= 128 or len(self.threads) >= 32:
                raise ValueError("Invalid tool child binding.")
            self.threads.add(value)
        elif operation in ("turn_started", "turn_finished") and set(message) == {"operation", "thread", "turn"}:
            thread, turn = message["thread"], message["turn"]
            if thread not in self.threads or not isinstance(turn, str) or not 0 < len(turn) <= 128:
                raise ValueError("Invalid tool turn binding.")
            if operation == "turn_started":
                if len(self.turns) >= 32:
                    raise ValueError("Too many active tool turns.")
                self.turns.add((thread, turn))
            else:
                self.turns.discard((thread, turn))
        elif operation == "deactivate" and set(message) == {"operation"}:
            self.scope = None
            self.epoch += 1
            self.turns.clear()
            self.threads.clear()
        else:
            raise ValueError("Invalid tool control.")
        self.changed.set()

    @asynccontextmanager
    async def admit(self, context):
        epoch = self.epoch
        try:
            identity = call_identity(context)
            until = time.monotonic() + .5
            while True:
                self.changed.clear()
                if self.scope is None or self.epoch != epoch or self.scope["deadline"] <= time.time():
                    raise ToolError("Tool request is inactive or expired.")
                if identity in self.turns:
                    break
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise ToolError("Tool call does not belong to an active turn.")
                try:
                    await asyncio.wait_for(self.changed.wait(), remaining)
                except TimeoutError:
                    raise ToolError("Tool call does not belong to an active turn.") from None
            snapshot = validate_scope(self.scope)
        except (ValueError, ToolError):
            self.rejected += 1
            raise
        self.inflight += 1
        self.calls += 1
        self.child_calls += identity[0] != self.parent
        binding = CALL_SCOPE.set(snapshot)
        try:
            yield
        finally:
            CALL_SCOPE.reset(binding)
            self.inflight -= 1
            self.changed.set()


class AuthenticatedHTTP:
    def __init__(self, app, token):
        self.app, self.authorization = app, ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [value for key, value in scope.get("headers", []) if key.lower() == b"authorization"]
            if len(values) != 1 or not hmac.compare_digest(values[0], self.authorization):
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
                return
        await self.app(scope, receive, send)


async def serve():
    # This executes after setpriv/exec and before the readiness acknowledgment exposes the helper.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise RuntimeError("Tool process isolation is unavailable.")
    import uvicorn

    admission = Admission()
    server = await make_scoped_server(current_scope)
    original = server.call_tool
    original_list, catalog_lists = server.list_tools, 0

    async def list_tools():
        nonlocal catalog_lists
        catalog_lists += 1
        return await original_list()

    async def call_tool(name, arguments, context=None):
        async with admission.admit(context):
            return await original(name, arguments, context)

    server.call_tool = call_tool
    server.list_tools = list_tools
    token = secrets.token_urlsafe(32)
    app = server.streamable_http_app(json_response=True, stateless_http=True, session_idle_timeout=None,
                                     max_request_body_size=1024 * 1024, host="127.0.0.1")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
    host = uvicorn.Server(uvicorn.Config(AuthenticatedHTTP(app, token), access_log=False, log_config=None,
                                        log_level="critical", lifespan="on"))
    hosting = asyncio.create_task(host.serve(sockets=[listener]))
    while not host.started:
        if hosting.done():
            await hosting
            raise RuntimeError("Tool transport did not start.")
        await asyncio.sleep(.005)
    print(json.dumps({"ready": {"url": endpoint, "token": token}, "isolated": True}), flush=True)
    reader = asyncio.StreamReader(limit=MAX_CONTROL)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    try:
        while frame := await reader.readline():
            if len(frame) > MAX_CONTROL:
                raise ValueError("Tool control exceeded its limit.")
            message = json.loads(frame)
            identifier = message.pop("id")
            operation = message.get("operation")
            admission.update(message)
            if operation == "deactivate":
                until = time.monotonic() + 1
                while admission.inflight and time.monotonic() < until:
                    admission.changed.clear()
                    try:
                        await asyncio.wait_for(admission.changed.wait(), max(.001, until - time.monotonic()))
                    except TimeoutError:
                        break
                if admission.inflight:
                    raise RuntimeError("Tool calls did not become quiescent.")
            print(json.dumps({"id": identifier, "ok": True, "inflight": admission.inflight,
                              "calls": admission.calls, "child_calls": admission.child_calls, "rejected": admission.rejected,
                              "catalog_lists": catalog_lists}), flush=True)
    finally:
        admission.scope = None
        transport.close()
        host.should_exit = True
        hosting.cancel()
        await asyncio.gather(hosting, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except BaseException:
        # Protocol errors may contain private control data; the supervisor owns safe diagnostics.
        sys.exit(1)
