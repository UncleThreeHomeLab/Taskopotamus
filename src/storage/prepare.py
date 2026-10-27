import argparse
import os
import secrets
from pathlib import Path

from src.runtime import private_directory


SECRET_NAMES = (
    "agent_api_token", "profile_signing_key", "postgres_admin_password",
    "agent_database_password", "profile_database_password", "telegram_database_password", "silo_root_password",
    "google_control_secret", "google_token_encryption_key",
)


def prepare(data_dir, credentials_dir=None, container=False):
    if Path(data_dir).is_symlink():
        raise ValueError("Storage directories cannot be symbolic links.")
    root = private_directory(data_dir)
    for name, owner in (("postgres", 999), ("objects", 10001), ("codex", 10001), ("legacy", 0), ("artifacts", 10001), ("google", 10001)):
        if (root / name).is_symlink():
            raise ValueError("Storage directories cannot be symbolic links.")
        directory = root / name
        if container or not directory.exists():
            private_directory(directory)
        elif not directory.is_dir():
            raise ValueError("Storage paths must be directories.")
        if container:
            os.chown(directory, owner, owner)
    if credentials_dir is not None:
        if Path(credentials_dir).is_symlink():
            raise ValueError("Credential directories cannot be symbolic links.")
        credentials = private_directory(credentials_dir)
        for name in SECRET_NAMES:
            path = credentials / name
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if path.is_symlink():
                    raise ValueError("Secret files cannot be symbolic links.")
                value = path.read_text().strip()
                if len(value) < 32 or not value.isascii():
                    raise ValueError("Existing secrets must contain at least 32 ASCII characters.")
            else:
                with os.fdopen(descriptor, "w") as target:
                    target.write(secrets.token_hex(32))
            if container:
                # Bind-mounted Compose secrets retain host ownership; only mounted files are exposed to services.
                os.chown(path, -1, 10001)
            path.chmod(0o640 if container else 0o600)
        from src.agent.worker_keys import prepare_keys
        prepare_keys(credentials, container)


def main():
    parser = argparse.ArgumentParser(description="Prepare private storage folders and generate missing service secrets without replacing existing secrets.")
    parser.add_argument("--data-dir", default="./storage")
    parser.add_argument("--credentials-dir")
    parser.add_argument("--container", action="store_true", help="Set Linux ownership for service folders; run only as container root.")
    args = parser.parse_args()
    prepare(args.data_dir, args.credentials_dir, args.container)
    print("Storage prepared. Existing secrets were preserved.")


if __name__ == "__main__":
    main()
