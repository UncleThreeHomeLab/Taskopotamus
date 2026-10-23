#!/usr/local/bin/python
"""Carry Codex stdio to the worker without forwarding controller secrets."""
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


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
    return [
        "ssh", "-T", "-F", "/dev/null", "-p", "2222",
        "-i", environment["TASKOPOTAMUS_SSH_KEY"],
        "-o", "UserKnownHostsFile=" + environment["TASKOPOTAMUS_SSH_HOSTS"],
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "LogLevel=ERROR",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        "-o", "SendEnv=TASKOPOTAMUS_PROFILE_AUTH", "root@worker",
        shlex.join(["/usr/local/bin/worker-codex", *arguments]),
    ]


def main():
    if not os.environ.get("TASKOPOTAMUS_SSH_KEY"):
        with configure_transport():
            return subprocess.call(command(sys.argv[1:], os.environ))
    arguments = command(sys.argv[1:], os.environ)
    # exec preserves signals and the SDK's stdio protocol; only the scoped profile token is sent by SSH.
    os.execvp(arguments[0], arguments)


if __name__ == "__main__":
    sys.exit(main())
