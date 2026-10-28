"""Privileged, bounded stdio proxy for a single retained native process."""
import json
import os
import select
import signal
import time
import uuid
from contextlib import contextmanager

from .request_scope import validate_scope


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
        self.idle_seconds = 300
        self.scope = lifecycle.state.parent / "runtime-scope" / "current.json"

    def pause(self):
        signal.pidfd_send_signal(self.pidfd, signal.SIGSTOP)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if self.child.poll() is not None:
                raise RuntimeError("Native runtime exited.")
            tasks = list((self.lifecycle.proc_root / str(self.child.pid) / "task").iterdir())
            states = [(task / "status").read_text() for task in tasks]
            if states and all("\nState:\tT" in value for value in states):
                return
            time.sleep(.005)
        raise RuntimeError("Native runtime did not pause.")

    def clear_scope(self):
        self.scope.unlink(missing_ok=True)

    def boundary(self):
        self.pause()
        self.clear_scope()
        self.lifecycle.stop_runtime_except(self.child.pid)

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
            if self.phase not in ("initial", "idle") or set(params) != {"scope", "idle_seconds"}:
                raise ValueError("Invalid runtime transition.")
            idle = params["idle_seconds"]
            if type(idle) is not int or not 10 <= idle <= 3600:
                raise ValueError("Invalid runtime lifetime.")
            scope = validate_scope(params["scope"])
            if scope["agent_ip"] != self.lifecycle.agent_ip:
                raise ValueError("Invalid runtime endpoint.")
            self.boundary()
            state = self.lifecycle.read_state()
            if state["cleaning"] or time.time() >= state["created_at"] + self.lifecycle.retention:
                raise RuntimeError("Workspace expired.")
            (self.lifecycle.state / "input-refusal.json").unlink(missing_ok=True)
            self.lifecycle.prepare(admission=True, retained_pid=self.child.pid)
            self.lifecycle.stage_inputs(scope["artifact_token"])
            self.lifecycle.directory(self.scope.parent, 0o755)
            self.lifecycle.write_scope(self.scope, scope)
            self.idle_seconds = idle
            self.deadline = time.monotonic() + scope["deadline"] - time.time()
            if self.deadline <= time.monotonic():
                raise TimeoutError("Runtime scope expired.")
            self.phase = "active"
            signal.pidfd_send_signal(self.pidfd, signal.SIGCONT)
            result = self.lifecycle.status()
        elif operation == "end":
            if self.phase != "active" or params != {"unloaded": True}:
                raise ValueError("Invalid runtime transition.")
            self.boundary()
            self.phase = "idle"
            state = self.lifecycle.read_state()
            remaining = state["created_at"] + self.lifecycle.retention - time.time()
            self.deadline = time.monotonic() + min(self.idle_seconds, remaining)
            result = {"paused": True}
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
                reads = [self.input_fd, self.native_out]
                writes = ([self.native_in] if self.to_native else []) + ([self.output_fd] if self.to_controller else [])
                readable, writable, _ = select.select(reads, writes, [], min(.1, remaining))
                for fd in writable:
                    buffer = self.to_native if fd == self.native_in else self.to_controller
                    try:
                        count = os.write(fd, buffer[:65536])
                        del buffer[:count]
                    except BlockingIOError:
                        pass
                for fd in readable:
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
