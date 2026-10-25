"""Bounded byte transport and delivery control; worker tokens only stage output."""
import json
import secrets
from urllib.parse import parse_qs, urlsplit

from .artifacts import ArtifactCapacityError
from .messages import opaque_id


class MessagingAPI:
    def __init__(self, deliveries, artifacts):
        self.deliveries, self.artifacts = deliveries, artifacts

    @staticmethod
    def body(handler, maximum=65536):
        if handler.headers.get("Transfer-Encoding") or len(handler.headers.get_all("Content-Length", [])) != 1:
            raise ValueError("Invalid request framing.")
        value = handler.headers["Content-Length"]
        if not value.isascii() or not value.isdigit() or not 0 < int(value) <= maximum:
            raise ValueError("Invalid request size.")
        return int(value)

    def json_body(self, handler):
        length = self.body(handler)
        value = json.loads(handler.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Expected an object.")
        return value

    @staticmethod
    def fields(payload, expected):
        if set(payload) != set(expected):
            raise ValueError("Invalid request fields.")

    def dispatch(self, handler, api_token):
        if not handler.path.startswith("/messaging/"):
            return False
        streaming = False
        try:
            headers = handler.headers.get_all("Authorization", [])
            if len(headers) != 1 or not headers[0].startswith("Bearer "):
                raise PermissionError()
            credential = headers[0][7:]
            worker = handler.path.startswith("/messaging/worker/")
            if worker:
                chat_id, request_id = self.artifacts.scope(credential)
            elif not secrets.compare_digest(credential.encode(), api_token.encode()):
                raise PermissionError()
            parts = urlsplit(handler.path)
            query = parse_qs(parts.query, strict_parsing=True)
            if handler.command == "POST" and parts.path in ("/messaging/upload", "/messaging/worker/upload"):
                metadata_headers = handler.headers.get_all("X-Artifact-Metadata", [])
                if len(metadata_headers) != 1 or len(metadata_headers[0]) > 4096 or query:
                    raise ValueError("Invalid upload metadata.")
                meta = json.loads(metadata_headers[0])
                expected = {"operation", "filename", "mime", "size", "sha256"}
                if worker:
                    self.fields(meta, expected)
                    direction = "output"
                else:
                    self.fields(meta, expected | {"chat_id", "request_id"})
                    chat_id, request_id, direction = meta["chat_id"], meta["request_id"], "input"
                length = self.body(handler, self.artifacts.policy.output_max_bytes if worker else self.artifacts.policy.input_max_bytes)
                if length != meta["size"]:
                    raise ValueError("Attachment size mismatch.")
                data = self.artifacts.upload(chat_id, request_id, direction, meta["operation"], meta["filename"], meta["mime"],
                                             meta["size"], meta["sha256"], handler.rfile, credential if worker else None)
            elif handler.command == "GET" and parts.path.startswith(("/messaging/artifact/", "/messaging/worker/artifact/")):
                artifact_id = opaque_id(parts.path.rsplit("/", 1)[-1])
                if worker:
                    if query:
                        raise ValueError("Invalid artifact query.")
                    info = self.artifacts.describe(artifact_id, chat_id, request_id)
                    if info["direction"] != "input":
                        raise PermissionError()
                    committed = False
                else:
                    if set(query) != {"chat_id"} or len(query["chat_id"]) != 1:
                        raise ValueError("A conversation is required.")
                    chat_id, request_id, committed = query["chat_id"][0], None, True
                info, file = self.artifacts.open(artifact_id, chat_id, request_id, committed)
                with file:
                    streaming = True
                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/octet-stream")
                    handler.send_header("Content-Length", str(info["size"]))
                    handler.send_header("Cache-Control", "no-store")
                    handler.send_header("X-Content-Type-Options", "nosniff")
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    handler.close_connection = True
                    while chunk := file.read(65536):
                        handler.wfile.write(chunk)
                return True
            elif handler.command == "POST" and not query:
                payload = self.json_body(handler)
                path = parts.path
                if worker:
                    if path != "/messaging/worker/batch":
                        raise PermissionError()
                    self.fields(payload, {"operation", "items"})
                    data = {"state": "prepared", "items": self.artifacts.prepare_batch(credential, payload["operation"], payload["items"])}
                elif path == "/messaging/claim":
                    self.fields(payload, {"allowed_chats"})
                    data = {"delivery": self.deliveries.claim(payload["allowed_chats"])}
                elif path == "/messaging/receipt":
                    self.fields(payload, {"delivery_id", "sequence", "attempt", "state", "receipt"})
                    self.deliveries.acknowledge(payload["delivery_id"], payload["sequence"], payload["attempt"], payload["state"], payload["receipt"])
                    data = {"ok": True}
                elif path == "/messaging/status":
                    self.fields(payload, {"chat_id"})
                    data = {"items": self.deliveries.status(payload["chat_id"])}
                elif path == "/messaging/lookup":
                    self.fields(payload, {"chat_id", "request_id"})
                    data = {"reply": self.deliveries.get(payload["chat_id"], payload["request_id"])}
                elif path == "/messaging/local-command":
                    self.fields(payload, {"chat_id", "request_id", "command"})
                    data = self.deliveries.reserve_command(payload["chat_id"], payload["request_id"], payload["command"])
                elif path == "/messaging/publish":
                    self.fields(payload, {"chat_id", "request_id", "text"} | ({"command_attempt"} if "command_attempt" in payload else set()))
                    if not isinstance(payload["text"], str):
                        raise ValueError("Reply text required.")
                    if "command_attempt" in payload:
                        data = self.deliveries.complete_command(payload["chat_id"], payload["request_id"], payload["command_attempt"], payload["text"])
                    else:
                        with self.deliveries.database.connect() as db:
                            self.deliveries.publish(db, payload["chat_id"], payload["request_id"], [{"type": "text", "text": payload["text"]}])
                            data = self.deliveries.get(payload["chat_id"], payload["request_id"], db)
                elif path == "/messaging/import-legacy":
                    self.fields(payload, {"chat_id", "request_id", "chunks", "next_chunk"})
                    data = {"delivery_id": self.deliveries.import_legacy(payload["chat_id"], payload["request_id"], payload["chunks"], payload["next_chunk"])}
                else:
                    handler.send_json({"error": "Not found"}, 404)
                    return True
            else:
                handler.send_json({"error": "Not found"}, 404)
                return True
            handler.send_json(data, 200)
        except PermissionError:
            handler.send_json({"error": "Attachment access denied"}, 403)
        except ArtifactCapacityError:
            handler.send_json({"error": "Attachment storage limit reached"}, 507)
        except (ValueError, TypeError, KeyError, UnicodeError):
            handler.send_json({"error": "Invalid messaging request"}, 400)
        except Exception:
            if not streaming:
                handler.send_json({"error": "Messaging request failed"}, 503)
        return True
