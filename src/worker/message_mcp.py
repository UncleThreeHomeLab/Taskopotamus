"""Scoped MCP messages: immutable text and streamed file snapshots for the current chat."""
import hashlib
import json
import mimetypes
import os
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import Annotated, Literal, Union

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .attachment_mcp import AttachmentClient, RequestUncertain, open_output, signature


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=64000)


class FilePart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["image", "file"]
    path: str = Field(min_length=1, max_length=4096)
    caption: str = Field(default="", max_length=16000)


MessagePart = Annotated[Union[TextPart, FilePart], Field(discriminator="type")]
PARTS = TypeAdapter(list[MessagePart])


class MessageClient(AttachmentClient):
    def send(self, message_id, items):
        import re

        if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", message_id):
            raise ValueError("Use a stable message_id containing 1–64 letters, digits, underscores, or hyphens.")
        parts = PARTS.validate_python(items)
        if not 1 <= len(parts) <= 31 or sum(isinstance(item, FilePart) for item in parts) > 10:
            raise ValueError("Supply 1–31 ordered parts with at most ten files.")
        arguments = [item.model_dump() for item in parts]
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        identity = {"message_id": message_id, "request_signature": digest}
        previous = self.request("/message-lookup", json.dumps(identity).encode(), {"Content-Type": "application/json"})["message"]
        if previous is not None:
            return self.summary(previous, message_id)
        with ExitStack() as stack:
            outgoing = []
            for index, part in enumerate(parts):
                if isinstance(part, TextPart):
                    outgoing.append(part.model_dump())
                    continue
                file = stack.enter_context(open_output(part.path))
                before, content = signature(file), hashlib.sha256()
                while chunk := file.read(65536):
                    content.update(chunk)
                if before != signature(file):
                    raise ValueError("File changed while preparing the message.")
                filename = PurePosixPath(part.path).name
                mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                if part.type == "image":
                    file.seek(0)
                    magic = file.read(8)
                    mime = "image/png" if magic == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if magic.startswith(b"\xff\xd8\xff") else ""
                    if not mime or before[2] > 10000000:
                        raise ValueError("Image previews require PNG/JPEG of at most 10 MB; use file otherwise.")
                meta = {"filename": filename, "mime": mime, "size": before[2], "sha256": content.hexdigest(),
                        "operation": hashlib.sha256(f"{message_id}:{digest}:{index}:{content.hexdigest()}".encode()).hexdigest()}
                file.seek(0)
                info = self.request("/upload", file, {"Content-Type": "application/octet-stream",
                                    "Content-Length": str(meta["size"]), "X-Artifact-Metadata": json.dumps(meta)})
                if before != signature(file):
                    raise ValueError("File changed during upload; no message was queued.")
                outgoing.append({"type": part.type, "artifact_id": info["artifact_id"], "caption": part.caption})
            result = self.request("/send", json.dumps({**identity, "items": outgoing}, ensure_ascii=False).encode(),
                                  {"Content-Type": "application/json"})
            return self.summary(result, message_id)

    @staticmethod
    def summary(result, message_id):
        return {"state": result["state"], "message_id": message_id, "delivery_id": result["delivery_id"],
                "parts": len(result["items"])}


def make_server(client):
    server = MCPServer("Taskopotamus Messages")

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=True))
    def send_message(message_id: str, items: list[MessagePart]) -> dict:
        """Queue ordered text, images with captions, or files to the current authenticated conversation.

        Use one stable message_id per new message/batch. Retry uncertain calls with IDENTICAL arguments and ID,
        including after source deletion. Never change an already queued message; use a new ID for a correction.
        A captioned image can stand alone. Multiple calls can send useful progress then results while work continues.
        Queued does not mean delivered or permanently saved. Recipient is fixed; do not send credentials/native files.
        Finish the native response without repeating user-facing content after sending messages here.
        """
        try:
            return client.send(message_id, items)
        except RequestUncertain:
            return {"state": "uncertain", "message_id": message_id,
                    "error": "Queue outcome uncertain. Retry identical arguments with the same message_id."}
        except (ValueError, OSError, RuntimeError):
            return {"state": "not_queued", "message_id": message_id,
                    "error": "Message rejected or file unavailable. Check supported parts, safe paths, limits, identity, and active run."}

    return server


def main():
    from .request_scope import credentials

    scope = credentials()
    client = MessageClient(scope["artifact_token"], scope["agent_ip"])
    make_server(client).run(transport="stdio")


if __name__ == "__main__":
    main()
