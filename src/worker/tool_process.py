"""Root-owned lifetime and private control channel for the fixed tool helper."""
import json
import os
import select
import signal
import subprocess
import sys
import time
import uuid


class ToolProcess:
    def __init__(self, lifecycle):
        command, environment, cwd = lifecycle.child_command("warm", [])
        self.child = subprocess.Popen(command[:-1] + [sys.executable, "-I", "-m", "taskopotamus_worker.warm_tools"],
                                      env=environment, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
        self.pidfd = os.pidfd_open(self.child.pid)
        self.input, self.output = self.child.stdin.fileno(), self.child.stdout.fileno()
        os.set_blocking(self.input, False)
        os.set_blocking(self.output, False)
        self.pending, self.received = bytearray(), bytearray()
        self.ready, self.responses = None, {}
        self.last_stats = {}

    def send(self, operation, **values):
        identifier = str(uuid.uuid4())
        frame = json.dumps({"id": identifier, "operation": operation, **values}).encode() + b"\n"
        if len(frame) > 16384 or len(self.pending) + len(frame) > 1024 * 1024:
            raise ValueError("Tool control exceeded its limit.")
        self.pending.extend(frame)
        return identifier

    def service(self, readable=(), writable=()):
        if self.child.poll() is not None:
            raise RuntimeError("Tool helper exited.")
        if self.input in writable and self.pending:
            try:
                del self.pending[:os.write(self.input, self.pending[:65536])]
            except BlockingIOError:
                pass
        if self.output in readable:
            try:
                chunk = os.read(self.output, 65536)
            except BlockingIOError:
                return
            if not chunk:
                raise EOFError("Tool helper closed.")
            self.received.extend(chunk)
            if len(self.received) > 65536:
                raise ValueError("Tool response exceeded its limit.")
            while b"\n" in self.received:
                frame, _, rest = self.received.partition(b"\n")
                self.received = bytearray(rest)
                value = json.loads(frame)
                if self.ready is None:
                    if set(value) != {"ready", "isolated"} or value["isolated"] is not True:
                        raise ValueError("Tool helper isolation was not confirmed.")
                    self.ready = value["ready"]
                elif isinstance(value, dict) and value.get("ok") is True and isinstance(value.get("id"), str):
                    self.responses[value["id"]] = value
                    if len(self.responses) > 128:
                        raise ValueError("Unexpected tool acknowledgments.")
                else:
                    raise ValueError("Invalid tool acknowledgment.")

    def wait(self, identifier=None, *, timeout=10, controller=None, buffer=None):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if identifier is None and self.ready is not None:
                return self.ready
            if identifier in self.responses:
                value = self.responses.pop(identifier)
                self.last_stats = {key: value[key] for key in ("calls", "child_calls", "rejected", "catalog_lists")
                                   if type(value.get(key)) is int}
                # Event acknowledgments preceding this barrier need no retained history.
                self.responses.clear()
                return value
            reads = [self.output] + ([controller] if controller is not None else [])
            readable, writable, _ = select.select(reads, [self.input] if self.pending else [], [], .05)
            if controller is not None and controller in readable:
                chunk = os.read(controller, 65536)
                if not chunk:
                    raise EOFError("Controller closed during tool transition.")
                buffer.extend(chunk)
                if len(buffer) > 1024 * 1024:
                    raise ValueError("Runtime input exceeded its limit.")
            self.service(readable, writable)
        raise TimeoutError("Tool helper transition timed out.")

    def request(self, operation, *, controller=None, buffer=None, **values):
        return self.wait(self.send(operation, **values), timeout=2, controller=controller, buffer=buffer)

    def continue_process(self):
        signal.pidfd_send_signal(self.pidfd, signal.SIGCONT)

    def close(self):
        try:
            if self.child.poll() is None:
                signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
            self.child.wait(timeout=3)
        finally:
            os.close(self.pidfd)
            self.child.stdin.close()
            self.child.stdout.close()
