import threading

from .device_login import start_device_login


class CodexAuth:
    """One shared Codex account for this personal deployment."""

    def __init__(self, codex):
        self.codex = codex
        self.lock = threading.Lock()
        self.pending = None
        self.failed = False

    def status(self):
        with self.lock:
            return self._status()

    def _status(self):
        if self.codex.account().account is not None:
            return {"state": "signed_in"}
        if self.pending is not None:
            return {"state": "pending"}
        return {"state": "failed" if self.failed else "signed_out"}

    def login(self, restart=False):
        with self.lock:
            if self._status()["state"] == "signed_in":
                return {"state": "signed_in"}
            if restart and self.pending is not None:
                self.pending.cancel()
                self.pending = None
            if self.pending is None:
                self.pending = start_device_login()
                self.failed = False
                threading.Thread(target=self._wait, args=(self.pending,), daemon=True).start()
            return {
                "state": "pending",
                "verification_url": self.pending.verification_url,
                "user_code": self.pending.user_code,
            }

    def _wait(self, handle):
        try:
            success = handle.wait()
        except Exception:
            # SDK exceptions may contain authentication details; never forward them.
            success = False
        with self.lock:
            # A cancelled attempt may finish after its replacement has started.
            if self.pending is handle:
                self.failed = not success
                self.pending = None
