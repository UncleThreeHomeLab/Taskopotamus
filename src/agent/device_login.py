"""Run native Codex device login without forwarding terminal output to logs."""

import os
import re
import subprocess
import threading

from codex_cli_bin import bundled_codex_path


def parse_instructions(line):
    # ponytail: CLI output parsing follows the pinned runtime; recheck it when upgrading Codex.
    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
    url = "https://auth.openai.com/codex/device"
    code = re.search(r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b", line)
    return url if url in line else None, code.group() if code else None


class DeviceLogin:
    def __init__(self):
        self.verification_url = None
        self.user_code = None
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.success = False
        self.process = subprocess.Popen(
            [os.environ.get("CODEX_BIN") or str(bundled_codex_path()), "-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            with self.process.stdout as output:
                for line in output:
                    url, code = parse_instructions(line)
                    if url:
                        self.verification_url = url
                    if code:
                        self.user_code = code
                    if self.verification_url and self.user_code:
                        self.ready.set()
            self.success = self.process.wait() == 0
        except (OSError, UnicodeError):
            self.cancel()
        finally:
            self.finished.set()
            self.ready.set()

    def wait(self):
        if not self.finished.wait(timeout=16 * 60):
            self.cancel()
            return False
        return self.success

    def cancel(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def start_device_login():
    login = DeviceLogin()
    if not login.ready.wait(timeout=30) or not login.verification_url or not login.user_code:
        login.cancel()
        raise RuntimeError("Codex CLI could not start device login.")
    return login
