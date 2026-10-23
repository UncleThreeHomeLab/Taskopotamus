import os
import subprocess
from pathlib import Path


def prepare_keys(directory, container=False):
    directory = Path(directory)
    for name in ("worker_client_key", "worker_host_key"):
        private = directory / name
        public = directory / (name + ".pub")
        if private.is_symlink() or public.is_symlink():
            raise ValueError("Worker keys cannot be symbolic links.")
        if not private.exists() and not public.exists() and not container:
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not private.is_file() or not public.is_file():
            raise ValueError("Prepare worker keys on the host before starting Compose.")
        if container:
            os.chown(private, -1, 10001)
        private.chmod(0o640 if container else 0o600)
        public.chmod(0o644)
