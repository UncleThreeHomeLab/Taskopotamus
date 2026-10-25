#!/usr/local/bin/python
"""Trusted worker supervision; model processes never run with supervisor privileges."""
import fcntl
import json
import ipaddress
import math
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


RUNTIME_UID = 10001
AUTH_UID = 10002
CODEX = "/usr/local/lib/python3.13/site-packages/codex_cli_bin/bin/codex"
MAX_CREDENTIAL_BYTES = 1024 * 1024


def positive_setting(env, name, default):
    value = env.get(name, str(default))
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 2**63 - 1:
        raise ValueError("Invalid worker lifecycle setting.")
    return int(value)


def directory(path, mode, uid=0):
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("Worker directory must not be a symlink.")
    os.chown(path, uid, uid)
    path.chmod(mode)


def atomic_write(path, content, uid=0):
    # Parent directories have fixed, non-writable ancestors; no user-supplied paths enter this function.
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            os.fchown(output.fileno(), uid, uid)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(Path(temporary).name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        os.fsync(parent)
    finally:
        if temporary is not None:
            os.unlink(temporary)
        os.close(parent)


def durable_unlink(path):
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink(path.name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.fsync(parent)
    finally:
        os.close(parent)


def read_json(path, limit=MAX_CREDENTIAL_BYTES):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid worker state file.")
        content = source.read(limit + 1)
        if len(content) > limit:
            raise ValueError("Invalid worker state file.")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("Invalid worker state file.")
        return value


def uid_processes(uid):
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines() if ":" in line)
            # A zombie thread-group leader can still have live threads writing files.
            active = not fields["State"].lstrip().startswith("Z") or int(fields["Threads"]) > 1
            if uid in [int(value) for value in fields["Uid"].split()] and active:
                result.append(int(entry.name))
        except FileNotFoundError:
            continue
    return result


def stop_uid(uid, grace=2):
    if uid == 0 or uid == os.getuid():
        raise ValueError("Refusing to stop supervisor identity.")
    for signum in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + grace
        while True:
            processes = uid_processes(uid)
            if not processes:
                return
            for pid in processes:
                try:
                    descriptor = os.pidfd_open(pid)
                    try:
                        if pid in uid_processes(uid):
                            signal.pidfd_send_signal(descriptor, signum)
                    finally:
                        os.close(descriptor)
                except (ProcessLookupError, FileNotFoundError):
                    pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    if uid_processes(uid):
        raise RuntimeError("Worker execution has not stopped.")


def wait_device_login(child, descriptor):
    """EOF requests cancellation; the supervisor acknowledges only after final cleanup."""
    while child.poll() is None:
        readable, _, _ = select.select([descriptor], [], [], 0.1)
        if readable:
            if os.read(descriptor, 1):
                raise ValueError("Device login input must remain empty.")
            return 76
    return 0 if child.returncode == 0 else 77


class WorkspaceLifecycle:
    def __init__(self, workspace=Path("/workspace"), saved=Path("/saved"), state=Path("/var/lib/taskopotamus/state"),
                 runtime_home=Path("/home/agent/.codex"), auth_home=Path("/var/lib/taskopotamus/auth/.codex"),
                 retention=604800, sweep=3600, max_bytes=1073741824, runtime_uid=RUNTIME_UID, auth_uid=AUTH_UID, agent_ip="10.203.17.30"):
        self.workspace, self.saved, self.state = Path(workspace), Path(saved), Path(state)
        self.runtime_home, self.auth_home = Path(runtime_home), Path(auth_home)
        self.retention, self.sweep, self.max_bytes = retention, sweep, max_bytes
        self.runtime_uid, self.auth_uid = runtime_uid, auth_uid
        address = ipaddress.IPv4Address(agent_ip)
        if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
            raise ValueError("Invalid private attachment service address.")
        self.agent_ip = str(address)
        self.epoch_file = self.state / "workspace.json"
        self.pending = self.state / "runtime-credentials-pending.json"

    @classmethod
    def configured(cls):
        return cls(retention=positive_setting(os.environ, "WORKSPACE_RETENTION_SECONDS", 604800),
                   sweep=positive_setting(os.environ, "WORKSPACE_SWEEP_SECONDS", 3600),
                   max_bytes=positive_setting(os.environ, "WORKSPACE_MAX_BYTES", 1073741824),
                   agent_ip=os.environ.get("WORKER_AGENT_IP", "10.203.17.30"))

    @contextmanager
    def lock(self, name):
        descriptor = os.open(self.state / (name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def write_state(self, value):
        atomic_write(self.epoch_file, json.dumps(value).encode())

    def read_state(self):
        value = read_json(self.epoch_file)
        if (set(value) != {"created_at", "generation", "cleaning"} or
                type(value["created_at"]) not in (int, float) or value["created_at"] <= 0 or
                not math.isfinite(value["created_at"]) or
                type(value["generation"]) is not int or value["generation"] < 0 or
                type(value["cleaning"]) is not bool):
            raise ValueError("Invalid workspace lifecycle state.")
        return value

    def initialize(self):
        directory(self.state, 0o700)
        directory(self.workspace, 0o700, self.runtime_uid)
        directory(self.saved, 0o700, self.runtime_uid)
        directory(self.runtime_home, 0o700, self.runtime_uid)
        directory(self.auth_home, 0o700, self.auth_uid)
        with self.lock("workspace"), self.lock("credentials"):
            stop_uid(self.runtime_uid)
            stop_uid(self.auth_uid)
            # Preserve native session paths and ownership without following runtime-created links.
            for root, dirs, files in os.walk(self.runtime_home, followlinks=False):
                for name in dirs + files:
                    os.chown(Path(root, name), self.runtime_uid, self.runtime_uid, follow_symlinks=False)
            if not self.epoch_file.exists():
                if any(self.workspace.iterdir()) or (self.state / "auth-initialized.json").exists() or (self.state / "policy.json").exists():
                    raise RuntimeError("Unclassified workspace requires operator review.")
                self.write_state({"created_at": time.time(), "generation": 0, "cleaning": False})
            else:
                self.read_state()
            initialized = self.state / "auth-initialized.json"
            if not initialized.exists():
                self.copy_credentials(self.runtime_home, self.auth_home, self.auth_uid)
                atomic_write(initialized, b'{"initialized":true}')
            else:
                if read_json(initialized) != {"initialized": True}:
                    raise ValueError("Invalid authentication initialization state.")
            self.recover_credentials()
            self.prepare()
            atomic_write(self.state / "policy.json", json.dumps({"retention": self.retention,
                         "sweep": self.sweep, "max_bytes": self.max_bytes, "agent_ip": self.agent_ip}).encode())

    def copy_credentials(self, source, destination, uid):
        # Only this inert JSON file crosses identities. Never transfer configuration or hooks.
        for path in (source, destination):
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("Invalid credential directory.")
        try:
            value = read_json(source / "auth.json")
        except FileNotFoundError:
            durable_unlink(destination / "auth.json")
            return
        atomic_write(destination / "auth.json", json.dumps(value).encode(), uid)

    def recover_credentials(self):
        if self.pending.exists() or self.pending.is_symlink():
            if read_json(self.pending) != {"runtime_active": True}:
                raise ValueError("Invalid credential recovery state.")
            stop_uid(self.runtime_uid)
            self.copy_credentials(self.runtime_home, self.auth_home, self.auth_uid)
            durable_unlink(self.pending)

    def begin_runtime_credentials(self):
        self.copy_credentials(self.auth_home, self.runtime_home, self.runtime_uid)
        atomic_write(self.pending, b'{"runtime_active":true}')

    def usage(self):
        total = 0
        for base in (self.workspace, self.saved):
            if not stat.S_ISDIR(base.lstat().st_mode):
                raise ValueError("Invalid storage root.")
            for root, dirs, files in os.walk(base, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
                for name in dirs + files:
                    total += Path(root, name).lstat().st_size
        return total

    def prepare(self, now=None, admission=False):
        stop_uid(self.runtime_uid)
        value = self.read_state()
        now = time.time() if now is None else now
        if value["cleaning"] or now >= value["created_at"] + self.retention:
            value["cleaning"] = True
            self.write_state(value)
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError("Safe workspace deletion is unavailable.")
            # Runtime UID is stopped and excluded by lock, so no path can change during traversal.
            for item in self.workspace.iterdir():
                if item.is_symlink() or not item.is_dir():
                    item.unlink()
                else:
                    shutil.rmtree(item)
            if any(self.workspace.iterdir()):
                raise RuntimeError("Workspace cleanup did not finish.")
            descriptor = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.write_state({"created_at": now, "generation": value["generation"] + 1, "cleaning": False})
            print("Workspace cleanup completed.", file=sys.stderr, flush=True)
        used = self.usage()
        atomic_write(self.state / "admission.json", json.dumps({"usage_bytes": used,
                     "admission_blocked": used >= self.max_bytes or (self.state / 'input-refusal.json').exists()}).encode())
        if admission and used >= self.max_bytes:
            raise RuntimeError("Workspace storage limit reached; operator cleanup is required.")

    def status(self):
        value = self.read_state()
        admission = read_json(self.state / "admission.json")
        if (set(admission) != {"usage_bytes", "admission_blocked"} or type(admission["usage_bytes"]) is not int or
                admission["usage_bytes"] < 0 or type(admission["admission_blocked"]) is not bool):
            raise ValueError("Invalid workspace admission state.")
        return {"generation": value["generation"], "expires_at": value["created_at"] + self.retention,
                "retention_seconds": self.retention, "sweep_seconds": self.sweep, "max_bytes": self.max_bytes,
                "cleaning": value["cleaning"], **admission}

    def maintenance(self):
        try:
            with self.lock("workspace"):
                self.prepare()
        except BlockingIOError:
            pass

    def child_command(self, mode, arguments):
        uid = self.runtime_uid if mode == "runtime" else self.auth_uid
        home = self.workspace / ".home" if mode == "runtime" else self.auth_home.parent / "home"
        directory(home, 0o700, uid)
        temporary = home / "tmp"
        directory(temporary, 0o700, uid)
        environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(home),
                       "TMPDIR": str(temporary), "XDG_CACHE_HOME": str(home / ".cache"),
                       "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
                       "PIP_CACHE_DIR": str(home / ".cache/pip"), "UV_CACHE_DIR": str(home / ".cache/uv"),
                       "npm_config_cache": str(home / ".cache/npm"), "PYTHONDONTWRITEBYTECODE": "1",
                       "CODEX_HOME": str(self.runtime_home if mode == "runtime" else self.auth_home),
                       "NO_COLOR": "1", "TERM": "dumb"}
        if mode == "runtime" and os.environ.get("TASKOPOTAMUS_PROFILE_AUTH"):
            environment["TASKOPOTAMUS_PROFILE_AUTH"] = os.environ["TASKOPOTAMUS_PROFILE_AUTH"]
        if mode == "runtime" and os.environ.get("TASKOPOTAMUS_ARTIFACT_AUTH"):
            token = os.environ["TASKOPOTAMUS_ARTIFACT_AUTH"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise ValueError("Invalid attachment capability.")
            environment["TASKOPOTAMUS_ARTIFACT_AUTH"] = token
            environment["TASKOPOTAMUS_AGENT_IP"] = self.agent_ip
        command = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--bounding-set=-all",
                   "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", CODEX, *arguments]
        return command, environment, self.workspace if mode == "runtime" else home

    def run(self, mode, arguments):
        from contextlib import ExitStack
        with ExitStack() as locks:
            if mode == "runtime":
                locks.enter_context(self.lock("workspace"))
            locks.enter_context(self.lock("credentials"))
            self.recover_credentials()
            stop_uid(self.auth_uid)
            if mode == "runtime":
                (self.state / "input-refusal.json").unlink(missing_ok=True)
                self.prepare(admission=True)
                token = os.environ.get("TASKOPOTAMUS_ARTIFACT_AUTH")
                if token:
                    from taskopotamus_worker.input_files import fetch_inputs, materialize_inputs

                    manifest, source = fetch_inputs(token, self.agent_ip)
                    try:
                        materialize_inputs(
                            manifest, source, self.workspace, self.max_bytes - self.usage(), self.runtime_uid
                        )
                    except OverflowError:
                        atomic_write(self.state / "input-refusal.json", b'{"blocked":true}')
                        atomic_write(
                            self.state / "admission.json",
                            json.dumps({"usage_bytes": self.usage(), "admission_blocked": True}).encode(),
                        )
                        raise RuntimeError("Input exceeds workspace storage limit.") from None
                self.begin_runtime_credentials()
            command, environment, cwd = self.child_command(mode, arguments)
            uid = self.runtime_uid if mode == "runtime" else self.auth_uid
            child = None
            previous = {}

            def interrupted(_signum, _frame):
                raise InterruptedError("Worker operation interrupted.")

            try:
                for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    previous[signum] = signal.signal(signum, interrupted)
                login = mode == "auth" and arguments[-2:] == ["login", "--device-auth"]
                child = subprocess.Popen(
                    command,
                    env=environment,
                    cwd=cwd,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL if login else None,
                )
                result = wait_device_login(child, sys.stdin.fileno()) if login else child.wait()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
                stop_uid(uid)
                if child is not None:
                    child.wait(timeout=5)
                if mode == "runtime":
                    self.recover_credentials()
        return result


def parse_remote(original):
    parts = shlex.split(original)
    if len(parts) < 2 or parts[0] != "/usr/local/bin/worker-codex":
        raise ValueError("Invalid worker command.")
    mode, arguments = parts[1], parts[2:]
    if mode == "status" and not arguments:
        return mode, arguments
    if mode not in ("runtime", "auth"):
        raise ValueError("Invalid worker mode.")
    # Controller only needs pinned app-server, version inspection, and device login.
    if len(arguments) >= 2 and arguments[0] in ("-c", "--config") and arguments[1] == 'cli_auth_credentials_store="file"':
        rest = arguments[2:]
    else:
        rest = arguments
    allowed = (["app-server", "--listen", "stdio://"], ["--version"])
    if rest not in allowed and not (mode == "auth" and rest == ["login", "--device-auth"]):
        raise ValueError("Unsupported worker command.")
    return mode, arguments


def serve(lifecycle):
    lifecycle.initialize()
    sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/taskopotamus_config"])
    def shutdown(_signum, _frame):
        sshd.terminate()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    next_sweep = time.monotonic() + lifecycle.sweep
    try:
        while sshd.poll() is None:
            time.sleep(min(1, lifecycle.sweep))
            if time.monotonic() >= next_sweep:
                try:
                    lifecycle.maintenance()
                except Exception:
                    print("Workspace maintenance failed; retry scheduled.", file=sys.stderr, flush=True)
                next_sweep = time.monotonic() + lifecycle.sweep
        return sshd.wait()
    finally:
        stop_uid(lifecycle.runtime_uid)
        stop_uid(lifecycle.auth_uid)
        if sshd.poll() is None:
            sshd.terminate()


def main():
    os.umask(0o077)
    if sys.argv[1:] == ["serve"]:
        return serve(WorkspaceLifecycle.configured())
    # sshd creates a clean login environment; use the supervisor's validated deployment policy.
    policy = read_json(Path("/var/lib/taskopotamus/state/policy.json"))
    if (set(policy) != {"retention", "sweep", "max_bytes", "agent_ip"}
            or any(type(policy[name]) is not int or policy[name] <= 0 for name in ("retention", "sweep", "max_bytes"))):
        raise ValueError("Invalid persisted worker policy.")
    lifecycle = WorkspaceLifecycle(**policy)
    if sys.argv[1:] == ["status"] and "SSH_ORIGINAL_COMMAND" not in os.environ:
        print(json.dumps(lifecycle.status()))
        return 0
    if sys.argv[1:] != ["remote"]:
        raise ValueError("Invalid supervisor operation.")
    mode, arguments = parse_remote(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if mode == "status":
        print(json.dumps(lifecycle.status()))
        return 0
    return lifecycle.run(mode, arguments)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BlockingIOError:
        print("Worker is busy.", file=sys.stderr)
        sys.exit(75)
    except Exception:
        print("Worker operation failed safely; operator inspection may be required.", file=sys.stderr)
        sys.exit(1)
