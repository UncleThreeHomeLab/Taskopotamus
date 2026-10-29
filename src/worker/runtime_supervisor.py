"""Privileged, bounded stdio proxy for a single retained native process."""
import json
import os
import select
import signal
import time
import uuid
from contextlib import contextmanager

from .request_scope import validate_scope
from .tool_process import ToolProcess


PREFIX = "taskopotamus/runtime/"
MAX_FRAME = 1024 * 1024
MAX_BUFFER = 4 * MAX_FRAME


@contextmanager
def operation_deadline(deadline):
    def expired(_signum, _frame):
        raise TimeoutError("Runtime operation deadline expired.")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(.001, deadline - time.time()))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class RuntimeProxy:
    def __init__(self, lifecycle, child, input_fd, output_fd):
        self.lifecycle, self.child = lifecycle, child
        self.input_fd, self.output_fd = input_fd, output_fd
        self.native_in, self.native_out = child.stdin.fileno(), child.stdout.fileno()
        self.pidfd = os.pidfd_open(child.pid)
        self.phase = "initial"
        self.initialized = False
        self.input = bytearray()
        self.to_native, self.to_controller = bytearray(), bytearray()
        self.native_output = bytearray()
        self.closing_output = b""
        state = lifecycle.read_state()
        self.deadline = time.monotonic() + min(30, state["created_at"] + lifecycle.retention - time.time())
        self.scope = lifecycle.state.parent / "runtime-scope" / "current.json"
        self.tools = None
        self.parent = None
        self.threads, self.turns, self.pending_starts = set(), set(), {}
        self.completed_turns = set()

    def pause(self, child=None, descriptor=None):
        child = self.child if child is None else child
        descriptor = self.pidfd if descriptor is None else descriptor
        signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if child.poll() is not None:
                raise RuntimeError("Retained runtime process exited.")
            tasks = list((self.lifecycle.proc_root / str(child.pid) / "task").iterdir())
            states = [(task / "status").read_text() for task in tasks]
            if states and all("\nState:\tT" in value for value in states):
                return
            time.sleep(.005)
        raise RuntimeError("Native runtime did not pause.")

    def clear_scope(self):
        self.scope.unlink(missing_ok=True)

    def boundary(self):
        self.pause()
        if self.tools is not None:
            if self.phase == "active":
                self.tools.request("deactivate", controller=self.input_fd, buffer=self.input)
            self.pause(self.tools.child, self.tools.pidfd)
        self.clear_scope()
        self.lifecycle.stop_runtime_except(self.retained())

    def retained(self):
        return {self.child.pid} | ({self.tools.child.pid} if self.tools is not None else set())

    def tool_event(self, operation, **values):
        if self.tools is not None:
            self.tools.send(operation, **values)

    def child_thread(self, identifier):
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 128 or identifier in self.threads:
            return
        if len(self.threads) >= 32:
            raise ValueError("Too many request-owned threads.")
        self.threads.add(identifier)
        self.tool_event("child", thread=identifier)

    def start_turn(self, thread, turn):
        if not isinstance(turn, str) or not 0 < len(turn) <= 128:
            raise ValueError("Invalid native turn identity.")
        identity = (thread, turn)
        if identity not in self.turns and identity not in self.completed_turns:
            self.turns.add(identity)
            self.tool_event("turn_started", thread=thread, turn=turn)

    def observe_native(self, frame):
        """Observe native DATA for correlation; never dispatch privileged operations from it."""
        value = json.loads(frame)
        if not isinstance(value, dict) or self.phase != "active" or self.parent is None:
            return
        request = self.pending_starts.pop(str(value.get("id")), None)
        if request is not None:
            result = value.get("result", {})
            if isinstance(result, dict) and isinstance(result.get("turn"), dict):
                self.start_turn(request, result["turn"].get("id"))
        method, params = value.get("method"), value.get("params", {})
        if not isinstance(params, dict):
            return
        thread = params.get("threadId")
        # A lineage notification alone never grants authority: it may be delayed
        # from an earlier request. Only current parent-turn activity proves a child.
        if method in ("item/started", "item/completed") and thread == self.parent:
            item = params.get("item", {})
            if ((thread, params.get("turnId")) in self.turns
                    and isinstance(item, dict) and item.get("type") == "subAgentActivity"
                    and item.get("kind") in ("started", "interacted")):
                self.child_thread(item.get("agentThreadId"))
        elif method in ("turn/started", "turn/completed"):
            turn = params.get("turn", {})
            turn = turn.get("id") if isinstance(turn, dict) else None
            identity = (thread, turn)
            if thread not in self.threads or not isinstance(turn, str):
                return
            if method == "turn/started":
                # Parent starts are authorized exclusively by the exact response
                # to a controller-originated turn/start. HTTP admission waits for
                # that response when native sends the notification first.
                if (identity in self.turns or (thread != self.parent
                        and any(owner == self.parent for owner, _ in self.turns))):
                    self.start_turn(thread, turn)
            else:
                if len(self.completed_turns) >= 4096:
                    raise ValueError("Too many completed native turns.")
                self.completed_turns.add(identity)
                self.turns.discard(identity)
                self.tool_event("turn_finished", thread=thread, turn=turn)

    def control(self, message):
        identifier = message.get("id")
        try:
            valid_id = isinstance(identifier, str) and str(uuid.UUID(identifier)) == identifier
        except ValueError:
            valid_id = False
        if (set(message) != {"id", "method", "params"} or not valid_id or not isinstance(message["params"], dict)):
            raise ValueError("Invalid runtime control.")
        operation, params = message["method"][len(PREFIX):], message["params"]
        if operation == "close" and not params:
            return message["id"]
        if operation == "begin":
            if self.phase not in ("initial", "idle") or set(params) != {"scope"}:
                raise ValueError("Invalid runtime transition.")
            scope = validate_scope(params["scope"])
            if scope["agent_ip"] != self.lifecycle.agent_ip:
                raise ValueError("Invalid runtime endpoint.")
            phases, phase_started = {}, time.monotonic()
            self.boundary()
            phases["boundary"] = round((time.monotonic() - phase_started) * 1000)
            phase_started = time.monotonic()
            state = self.lifecycle.read_state()
            if state["cleaning"] or time.time() >= state["created_at"] + self.lifecycle.retention:
                raise RuntimeError("Workspace expired.")
            (self.lifecycle.state / "input-refusal.json").unlink(missing_ok=True)
            self.lifecycle.prepare(admission=True, retained_pid=self.retained())
            phases["prepare"] = round((time.monotonic() - phase_started) * 1000)
            phase_started = time.monotonic()
            self.lifecycle.stage_inputs(scope["artifact_token"])
            phases["stage_inputs"] = round((time.monotonic() - phase_started) * 1000)
            phase_started = time.monotonic()
            self.lifecycle.directory(self.scope.parent, 0o755)
            self.lifecycle.write_scope(self.scope, scope)
            phases["write_scope"] = round((time.monotonic() - phase_started) * 1000)
            phase_started = time.monotonic()
            if self.tools is None:
                self.tools = ToolProcess(self.lifecycle)
                self.tools.wait(controller=self.input_fd, buffer=self.input)
            else:
                self.tools.continue_process()
            self.tools.request("activate", scope=scope, controller=self.input_fd, buffer=self.input)
            self.parent = None
            self.threads.clear()
            self.turns.clear()
            self.pending_starts.clear()
            self.completed_turns.clear()
            self.deadline = time.monotonic() + scope["deadline"] - time.time()
            if self.deadline <= time.monotonic():
                raise TimeoutError("Runtime scope expired.")
            self.phase = "active"
            signal.pidfd_send_signal(self.pidfd, signal.SIGCONT)
            phases["helper_transition"] = round((time.monotonic() - phase_started) * 1000)
            result = self.lifecycle.status()
            result["tools"] = self.tools.ready
            result["phase_timings_ms"] = phases
        elif operation == "bind":
            if self.phase != "active" or set(params) != {"thread"} or self.parent is not None:
                raise ValueError("Invalid conversation binding.")
            parent = params["thread"]
            if not isinstance(parent, str) or not 0 < len(parent) <= 128:
                raise ValueError("Invalid conversation identity.")
            self.tools.request("bind", thread=parent, controller=self.input_fd, buffer=self.input)
            self.parent = parent
            self.threads.add(parent)
            result = {"bound": True}
        elif operation == "end":
            if (self.phase != "active" or params not in ({"unloaded": True}, {"quiescent": True})
                    or (params == {"quiescent": True} and (self.parent is None or self.turns or self.pending_starts))):
                raise ValueError("Invalid runtime transition.")
            self.boundary()
            self.phase = "idle"
            state = self.lifecycle.read_state()
            remaining = state["created_at"] + self.lifecycle.retention - time.time()
            self.deadline = time.monotonic() + remaining
            result = {"paused": True, "tools": self.tools.last_stats if self.tools else {}}
        else:
            raise ValueError("Invalid runtime control.")
        self.to_controller.extend(json.dumps({"id": message["id"], "result": result}).encode() + b"\n")
        return None

    def controller_frame(self, frame):
        value = json.loads(frame)
        if not isinstance(value, dict):
            raise ValueError("Invalid runtime message.")
        method = value.get("method")
        if isinstance(method, str) and method.startswith(PREFIX):
            if len(frame) > 8192 or self.to_native:
                raise ValueError("Invalid runtime control framing.")
            deadline = time.time() + 10
            if method == PREFIX + "begin":
                deadline = validate_scope(value.get("params", {}).get("scope"))["deadline"]
            with operation_deadline(deadline):
                return self.control(value)
        if self.phase == "initial":
            if method == "initialize" and not self.initialized:
                self.initialized = True
            elif method != "initialized" or not self.initialized:
                raise ValueError("Runtime scope is not active.")
        elif self.phase != "active":
            raise ValueError("Runtime scope is not active.")
        if method == "turn/start" and self.parent is not None:
            params = value.get("params", {})
            if not isinstance(params, dict) or params.get("threadId") != self.parent:
                raise ValueError("Turn does not belong to the bound conversation.")
            self.pending_starts[str(value.get("id"))] = self.parent
        self.to_native.extend(frame + b"\n")
        return None

    def run(self):
        descriptors = (self.input_fd, self.output_fd, self.native_in, self.native_out)
        previous = {fd: os.get_blocking(fd) for fd in descriptors}
        try:
            for fd in descriptors:
                os.set_blocking(fd, False)
            while self.child.poll() is None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if max(len(self.input), len(self.to_native), len(self.to_controller), len(self.native_output)) > MAX_BUFFER:
                    raise ValueError("Runtime transport exceeded its limit.")
                reads = [self.input_fd, self.native_out] + ([self.tools.output] if self.tools else [])
                writes = ([self.native_in] if self.to_native else []) + ([self.output_fd] if self.to_controller else [])
                if self.tools and self.tools.pending:
                    writes.append(self.tools.input)
                readable, writable, _ = select.select(reads, writes, [], min(.1, remaining))
                if self.tools:
                    self.tools.service(readable, writable)
                for fd in writable:
                    if self.tools and fd == self.tools.input:
                        continue
                    buffer = self.to_native if fd == self.native_in else self.to_controller
                    try:
                        count = os.write(fd, buffer[:65536])
                        del buffer[:count]
                    except BlockingIOError:
                        pass
                for fd in readable:
                    if self.tools and fd == self.tools.output:
                        continue
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        return None
                    if fd == self.native_out:
                        # Native output is data. It never enters the privileged control parser.
                        self.native_output.extend(chunk)
                        while b"\n" in self.native_output:
                            frame, _, rest = self.native_output.partition(b"\n")
                            self.native_output = bytearray(rest)
                            self.observe_native(frame)
                            self.to_controller.extend(frame + b"\n")
                    else:
                        self.input.extend(chunk)
                # Preserve write order when initialize/initialized/begin arrive in one pipe read.
                while b"\n" in self.input and not self.to_native:
                    frame, _, rest = self.input.partition(b"\n")
                    self.input = bytearray(rest)
                    if not frame or len(frame) > MAX_FRAME:
                        raise ValueError("Invalid runtime frame.")
                    close_id = self.controller_frame(frame)
                    if close_id is not None:
                        self.closing_output = bytes(self.to_controller)
                        return close_id
                if len(self.input) > MAX_FRAME:
                    raise ValueError("Runtime frame exceeded its limit.")
            return None
        finally:
            self.clear_scope()
            if self.tools is not None:
                self.tools.close()
            os.close(self.pidfd)
            for fd, blocking in previous.items():
                try:
                    os.set_blocking(fd, blocking)
                except OSError:
                    pass


def close_acknowledgment(descriptor, request_id, prefix=b""):
    """Acknowledge only after native descendants and credential checkout are cleaned."""
    buffer = prefix + json.dumps({"id": request_id, "result": {"closed": True}}).encode() + b"\n"
    blocking = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
        deadline = time.monotonic() + 3
        while buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Runtime close acknowledgment unavailable.")
            if select.select([], [descriptor], [], min(.1, remaining))[1]:
                try:
                    buffer = buffer[os.write(descriptor, buffer):]
                except BlockingIOError:
                    pass
    finally:
        os.set_blocking(descriptor, blocking)
