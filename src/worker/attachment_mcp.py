"""Worker-local MCP: snapshot approved files into the active reply, never send them."""
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import stat
from contextlib import ExitStack
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=4096)
    presentation: str = Field(pattern="^(image|file)$")
    caption: str = Field(default="", max_length=16000)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def open_output(path, roots=("/workspace", "/saved")):
    """Retain no-follow descriptors through traversal, hashing, and upload."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ValueError("Use an absolute file path under /workspace or /saved.")
    root = next((value for value in roots if path.startswith(value + "/")), None)
    if root is None or any(part.startswith(".") or part in ("", "..") for part in path.split("/")[1:]):
        raise ValueError("Runtime, hidden, and outside paths cannot be attached.")
    relative = path[len(root) + 1:].split("/")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        target = os.open(relative[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    file = os.fdopen(target, "rb")
    details = os.fstat(file.fileno())
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or not 0 < details.st_size <= 50000000:
        file.close()
        raise ValueError("Attach a regular, nonempty file of at most 50 MB without hard links.")
    return file


def signature(file):
    details = os.fstat(file.fileno())
    return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns


class AttachmentClient:
    def __init__(self, token, address):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError("Active reply capability is unavailable.")
        ip = ipaddress.IPv4Address(address)
        if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            raise ValueError("Invalid private attachment endpoint.")
        self.token, self.url = token, f"http://{ip}:8080/messaging/worker"
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, path, data, headers=None):
        request = Request(self.url + path, data=data, method="POST", headers={
            "Authorization": "Bearer " + self.token, **(headers or {}),
        })
        try:
            with self.opener.open(request, timeout=60) as response:
                result = response.read(2 * 1024 * 1024 + 1)
            if len(result) > 2 * 1024 * 1024:
                raise ValueError("Attachment response exceeded its limit.")
            return json.loads(result)
        except HTTPError as error:
            code = error.code
            error.close()
            raise RuntimeError(f"Attachment preparation rejected (HTTP {code}). Check files, version, limits, and active run.") from None
        except (URLError, OSError, ValueError):
            raise RuntimeError("Attachment preparation could not be confirmed. Retry the identical batch before changing it.") from None

    def attach(self, files, expected_version=0):
        if type(expected_version) is not int or expected_version < 0 or len(files) > 10 or not files and expected_version == 0:
            raise ValueError("Supply up to ten files, or a current version to clear attachments.")
        files = [entry.model_dump() if isinstance(entry, Attachment) else Attachment.model_validate(entry).model_dump() for entry in files]
        request_key = hashlib.sha256(json.dumps([expected_version, files], sort_keys=True).encode()).hexdigest()
        previous = self.request("/batch-lookup", json.dumps({"expected_version": expected_version, "request_key": request_key}).encode(),
                                {"Content-Type": "application/json"})["batch"]
        if previous is not None:
            return self.summary(previous, files)
        with ExitStack() as stack:
            snapshots, descriptions = [], []
            for entry in files:
                entry = entry.model_dump() if isinstance(entry, Attachment) else Attachment.model_validate(entry).model_dump()
                file = stack.enter_context(open_output(entry["path"]))
                before = signature(file)
                digest = hashlib.sha256()
                while chunk := file.read(65536):
                    digest.update(chunk)
                if before != signature(file):
                    raise ValueError("File changed while preparing the attachment.")
                filename = PurePosixPath(entry["path"]).name
                mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                if entry["presentation"] == "image":
                    file.seek(0)
                    magic = file.read(8)
                    mime = "image/png" if magic == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if magic.startswith(b"\xff\xd8\xff") else ""
                    if not mime or before[2] > 10000000:
                        raise ValueError("Inline images must be PNG or JPEG of at most 10 MB; use file presentation otherwise.")
                description = {"filename": filename, "mime": mime, "size": before[2], "sha256": digest.hexdigest(),
                               "presentation": entry["presentation"], "caption": entry["caption"]}
                descriptions.append(description)
                snapshots.append((file, before))
            operation = hashlib.sha256(json.dumps([expected_version, descriptions], sort_keys=True).encode()).hexdigest()
            items = []
            for index, (description, snapshot) in enumerate(zip(descriptions, snapshots)):
                file, before = snapshot
                file.seek(0)
                meta = {key: description[key] for key in ("filename", "mime", "size", "sha256")}
                meta["operation"] = hashlib.sha256(f"{operation}:{index}".encode()).hexdigest()
                info = self.request("/upload", file, {"X-Artifact-Metadata": json.dumps(meta), "Content-Length": str(meta["size"]),
                                                     "Content-Type": "application/octet-stream"})
                if before != signature(file):
                    raise ValueError("File changed during attachment upload; no new batch was prepared.")
                items.append({"type": description["presentation"], "artifact_id": info["artifact_id"], "caption": description["caption"]})
            result = self.request("/batch", json.dumps({"operation": operation, "expected_version": expected_version, "request_key": request_key, "items": items}, ensure_ascii=False).encode(),
                                  {"Content-Type": "application/json"})
            return self.summary(result, files)

    @staticmethod
    def summary(result, files):
        return {key: result[key] for key in ("state", "version", "current_version")} | {
            "files": [{"filename": PurePosixPath(d["path"]).name, "presentation": d["presentation"], "artifact_id": item["artifact_id"]}
                      for d, item in zip(files, result["items"])]}


def make_server(client):
    server = MCPServer("Taskopotamus Reply Attachments")

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    def attach_to_reply(files: list[Attachment], expected_version: int = 0) -> dict:
        """Prepare all verified files for this reply in one call. This does not send or permanently save files.

        Use image for PNG/JPEG previews or file for original downloads. Captions are optional.
        Initial expected_version is 0. To replace or clear the pending batch, supply its returned current version;
        files=[] clears it. Retry an uncertain call with identical files and version. Only the latest batch is published
        after successful completion. Never claim delivery from prepared state. Recipient is the current authenticated chat.
        """
        try:
            return client.attach(files, expected_version)
        except (ValueError, OSError, RuntimeError) as error:
            return {"state": "not_prepared", "error": str(error) if not isinstance(error, OSError) else "File unavailable or unsafe."}

    return server


def main():
    client = AttachmentClient(os.environ.get("TASKOPOTAMUS_ARTIFACT_AUTH", ""), os.environ.get("TASKOPOTAMUS_AGENT_IP", ""))
    make_server(client).run(transport="stdio")


if __name__ == "__main__":
    main()
