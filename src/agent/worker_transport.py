#!/usr/local/bin/python
"""Carry Codex stdio to the worker without forwarding controller secrets."""
import os
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


class WorkspaceStorageError(RuntimeError):
    pass


def configure_transport():
    directory = tempfile.TemporaryDirectory(prefix="taskopotamus-ssh-")
    root = Path(directory.name)
    key = root / "client_key"
    key.write_bytes(Path(os.environ["WORKER_CLIENT_KEY_FILE"]).read_bytes())
    key.chmod(0o600)
    host_key = Path(os.environ["WORKER_HOST_PUBLIC_KEY_FILE"]).read_text().strip()
    if not host_key.startswith("ssh-ed25519 ") or "\n" in host_key:
        directory.cleanup()
        raise ValueError("Invalid worker host public key.")
    known_hosts = root / "known_hosts"
    known_hosts.write_text("[worker]:2222 " + host_key + "\n")
    os.environ["TASKOPOTAMUS_SSH_KEY"] = str(key)
    os.environ["TASKOPOTAMUS_SSH_HOSTS"] = str(known_hosts)
    return directory


def command(arguments, environment):
    mode = environment.get("TASKOPOTAMUS_WORKER_MODE")
    if mode not in ("auth", "runtime", "status") or (mode == "status" and arguments):
        raise ValueError("Explicit worker mode is required.")
    return [
        "ssh", "-T", "-F", "/dev/null", "-p", "2222",
        "-i", environment["TASKOPOTAMUS_SSH_KEY"],
        "-o", "UserKnownHostsFile=" + environment["TASKOPOTAMUS_SSH_HOSTS"],
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "LogLevel=ERROR",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        "-o", "SendEnv=TASKOPOTAMUS_PROFILE_AUTH", "-o", "SendEnv=TASKOPOTAMUS_ARTIFACT_AUTH", "root@worker",
        shlex.join(["/usr/local/bin/worker-codex", mode, *arguments]),
    ]


def workspace_status():
    result = subprocess.run(command([], {**os.environ, "TASKOPOTAMUS_WORKER_MODE": "status"}),
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise RuntimeError("Workspace status unavailable.")
    status = json.loads(result.stdout)
    if (not isinstance(status, dict) or type(status.get("generation")) is not int or status["generation"] < 0 or
            type(status.get("retention_seconds")) is not int or status["retention_seconds"] <= 0 or
            type(status.get("expires_at")) not in (int, float) or status.get("cleaning") is not False):
        raise ValueError("Invalid workspace status.")
    if type(status.get("admission_blocked")) is not bool or type(status.get("usage_bytes")) is not int:
        raise ValueError("Invalid workspace admission status.")
    return {key: status[key] for key in ("generation", "retention_seconds", "expires_at", "admission_blocked", "usage_bytes")}


def main():
    if not os.environ.get("TASKOPOTAMUS_SSH_KEY"):
        with configure_transport():
            return subprocess.call(command(sys.argv[1:], os.environ))
    arguments = command(sys.argv[1:], os.environ)
    # exec preserves signals and the SDK's stdio protocol; only the scoped profile token is sent by SSH.
    os.execvp(arguments[0], arguments)


if __name__ == "__main__":
    sys.exit(main())
