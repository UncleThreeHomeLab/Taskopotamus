"""Copy authenticated input bytes under the existing workspace lease; never decode them."""
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_INPUT_BYTES = 20000000


def request_tag(chat_id, request_id):
    return hashlib.sha256(json.dumps([chat_id, request_id]).encode()).hexdigest()


def input_name(info):
    identifier = info.get("artifact_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid input identifier.")
    return identifier + {"image/png": ".png", "image/jpeg": ".jpg"}.get(info.get("presentation_mime"), ".bin")


def input_path(tag, info):
    if not re.fullmatch(r"[a-f0-9]{64}", tag):
        raise ValueError("Invalid input request.")
    return "/workspace/.incoming/" + tag + "/" + input_name(info)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch_inputs(token, address):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("Invalid input capability.")
    address = ipaddress.IPv4Address(address)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError("Invalid input endpoint.")
    opener = build_opener(ProxyHandler({}), NoRedirects())
    base = f"http://{address}:8080/messaging/worker/"

    def open_url(suffix):
        return opener.open(Request(base + suffix, headers={"Authorization": "Bearer " + token}), timeout=10)

    with open_url("inputs") as response:
        body = response.read(65537)
    if len(body) > 65536:
        raise ValueError("Input manifest exceeds limit.")
    return json.loads(body), lambda info: open_url("artifact/" + info["artifact_id"])


def materialize_inputs(manifest, source, workspace, remaining, runtime_gid=10001, owner_uid=0):
    tag, files = manifest.get("request_tag", ""), manifest.get("files")
    if not re.fullmatch(r"[a-f0-9]{64}", tag) or not isinstance(files, list) or len(files) > 10:
        raise ValueError("Invalid input manifest.")
    if not files:
        return
    total, names = 0, set()
    for info in files:
        name = input_name(info)
        if (
            name in names
            or type(info.get("size")) is not int
            or not 0 < info["size"] <= MAX_INPUT_BYTES
            or not re.fullmatch(r"[a-f0-9]{64}", info.get("sha256", ""))
        ):
            raise ValueError("Invalid input metadata.")
        names.add(name)
        total += info["size"]
    if total > MAX_INPUT_BYTES:
        raise ValueError("Combined inputs exceed 20 MB.")
    root = Path(workspace)
    workspace_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    incoming_fd = None
    temporary = None
    try:
        try:
            os.mkdir(".incoming", 0o750, dir_fd=workspace_fd)
            os.chown(".incoming", owner_uid, runtime_gid, dir_fd=workspace_fd, follow_symlinks=False)
        except FileExistsError:
            pass
        incoming_fd = os.open(".incoming", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=workspace_fd)
        details = os.fstat(incoming_fd)
        if details.st_uid != owner_uid or details.st_gid != runtime_gid or details.st_mode & 0o027:
            raise ValueError("Unsafe reserved input directory.")
        os.fchmod(incoming_fd, 0o750)
        try:
            existing = os.open(tag, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=incoming_fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            try:
                existing_details = os.fstat(existing)
                if (
                    existing_details.st_uid != owner_uid
                    or existing_details.st_gid != runtime_gid
                    or existing_details.st_mode & 0o777 != 0o750
                    or set(os.listdir(existing)) != names
                ):
                    raise ValueError("Invalid existing input directory.")
                for info in files:
                    descriptor = os.open(input_name(info), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=existing)
                    with os.fdopen(descriptor, "rb") as file:
                        details = os.fstat(file.fileno())
                        if (
                            not stat.S_ISREG(details.st_mode)
                            or details.st_uid != owner_uid
                            or details.st_gid != runtime_gid
                            or details.st_nlink != 1
                            or details.st_mode & 0o777 != 0o440
                        ):
                            raise ValueError("Unsafe existing input file.")
                        digest = hashlib.sha256()
                        while chunk := file.read(65536):
                            digest.update(chunk)
                        if details.st_size != info["size"] or digest.hexdigest() != info["sha256"]:
                            raise ValueError("Existing input changed.")
                return
            finally:
                os.close(existing)
        if total > remaining:
            raise OverflowError("Inputs exceed remaining workspace capacity.")
        temporary = "pending-" + uuid.uuid4().hex
        os.mkdir(temporary, 0o750, dir_fd=incoming_fd)
        os.chown(temporary, owner_uid, runtime_gid, dir_fd=incoming_fd, follow_symlinks=False)
        staging = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=incoming_fd)
        try:
            os.fchmod(staging, 0o750)
            for info in files:
                descriptor = os.open(
                    input_name(info), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging
                )
                with os.fdopen(descriptor, "wb") as file, source(info) as incoming:
                    digest, left = hashlib.sha256(), info["size"]
                    while left:
                        chunk = incoming.read(min(65536, left))
                        if not chunk or len(chunk) > left:
                            raise ValueError("Incomplete input transfer.")
                        file.write(chunk)
                        digest.update(chunk)
                        left -= len(chunk)
                    if incoming.read(1) or digest.hexdigest() != info["sha256"]:
                        raise ValueError("Input bytes failed verification.")
                    file.flush()
                    os.fsync(file.fileno())
                    os.fchown(file.fileno(), owner_uid, runtime_gid)
                    os.fchmod(file.fileno(), 0o440)
            os.fsync(staging)
        finally:
            os.close(staging)
        os.rename(temporary, tag, src_dir_fd=incoming_fd, dst_dir_fd=incoming_fd)
        temporary = None
        os.fsync(incoming_fd)
    finally:
        if temporary is not None:
            # UID writers are stopped and this directory is supervisor-created.
            shutil.rmtree(root / ".incoming" / temporary)
        if incoming_fd is not None:
            os.close(incoming_fd)
        os.close(workspace_fd)
