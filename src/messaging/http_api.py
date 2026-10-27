"""Bounded byte transport and delivery control; worker tokens only stage output."""
import json
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from .artifacts import ArtifactCapacityError
from .messages import opaque_id
from .message_publication import MessagePublication


class MessagingAPI:
    def __init__(self, deliveries, artifacts):
        self.deliveries, self.artifacts = deliveries, artifacts
        self.messages = MessagePublication(artifacts, deliveries)

    @staticmethod
    def body(handler, maximum=65536):
        if handler.headers.get("Transfer-Encoding") or len(handler.headers.get_all("Content-Length", [])) != 1:
            raise ValueError("Invalid request framing.")
        value = handler.headers["Content-Length"]
        if not value.isascii() or not value.isdigit() or not 0 < int(value) <= maximum:
            raise ValueError("Invalid request size.")
        return int(value)

    def json_body(self, handler, maximum=65536):
        length = self.body(handler, maximum)
        value = json.loads(handler.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Expected an object.")
        return value

    @staticmethod
    def fields(payload, expected):
        if set(payload) != set(expected):
            raise ValueError("Invalid request fields.")

    @staticmethod
    def transfer_deadline(handler):
        handler.read_deadline.cancel()
        handler.read_deadline = threading.Timer(90, handler.expire_request)
        handler.read_deadline.daemon = True
        handler.read_deadline.start()

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
            if handler.command == 'GET' and parts.path == '/messaging/worker/inputs' and worker and not query:
                handler.send_json(self.artifacts.input_manifest(credential),200)
                return True
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
                self.transfer_deadline(handler)
                data = self.artifacts.upload(chat_id, request_id, direction, meta["operation"], meta["filename"], meta["mime"],
                                             meta["size"], meta["sha256"], handler.rfile, credential if worker else None)
            elif handler.command == "GET" and parts.path.startswith(("/messaging/artifact/", "/messaging/worker/artifact/", "/messaging/metadata/")):
                artifact_id = opaque_id(parts.path.rsplit("/", 1)[-1])
                if worker:
                    if query:
                        raise ValueError("Invalid artifact query.")
                    info = self.artifacts.describe(artifact_id, chat_id, request_id)
                    if info["direction"] != "input":
                        raise PermissionError()
                    self.artifacts.accepted_input(credential,artifact_id)
                    committed = False
                else:
                    if set(query) != {"chat_id"} or len(query["chat_id"]) != 1:
                        raise ValueError("A conversation is required.")
                    chat_id, request_id, committed = query["chat_id"][0], None, True
                if parts.path.startswith("/messaging/metadata/"):
                    handler.send_json(self.artifacts.describe(artifact_id, chat_id, committed=True), 200)
                    return True
                info, file = self.artifacts.open(artifact_id, chat_id, request_id, committed)
                with file:
                    self.transfer_deadline(handler)
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
                payload = self.json_body(handler, 2 * 1024 * 1024 if worker else 65536)
                path = parts.path
                if worker:
                    if path == "/messaging/worker/message-lookup":
                        self.fields(payload, {"message_id", "request_signature"})
                        data = {"message": self.messages.lookup(credential, payload["message_id"], payload["request_signature"])}
                    elif path == "/messaging/worker/send":
                        self.fields(payload, {"message_id", "request_signature", "items"})
                        data = self.messages.send(credential, payload["message_id"], payload["request_signature"], payload["items"])
                    elif path == "/messaging/worker/batch-lookup":
                        self.fields(payload, {"expected_version", "request_key"})
                        data = {"batch": self.artifacts.batch_lookup(credential, payload["expected_version"], payload["request_key"])}
                    elif path == "/messaging/worker/batch":
                        self.fields(payload, {"operation", "items", "expected_version", "request_key"})
                        data = self.artifacts.prepare_batch(credential, payload["operation"], payload["items"], payload["expected_version"], payload["request_key"])
                        self.artifacts.maintain()
                    else:
                        raise PermissionError()
                elif path == "/messaging/claim":
                    self.fields(payload, {"allowed_chats"})
                    data = {"delivery": self.deliveries.claim(payload["allowed_chats"])}
                    delivery = data["delivery"]
                    if delivery and delivery["item"]["type"] in ("image", "file"):
                        delivery["artifact"] = self.artifacts.describe(delivery["item"]["artifact_id"], delivery["chat_id"], committed=True)
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
                elif path == "/messaging/snapshot":
                    self.fields(payload, {"chat_id", "request_id"})
                    reply = self.deliveries.snapshot(payload["chat_id"], payload["request_id"])
                    if reply:
                        reply["artifacts"] = {}
                        for item in reply["items"]:
                            if item["type"] in ("image", "file"):
                                try:
                                    reply["artifacts"][item["artifact_id"]] = self.artifacts.describe(
                                        item["artifact_id"], payload["chat_id"], committed=True
                                    )
                                except PermissionError:
                                    reply["artifacts"][item["artifact_id"]] = {"available": False}
                    data = {"reply": reply}
                elif path == '/messaging/input-lookup':
                    self.fields(payload,{'chat_id','request_id','operation'})
                    data = {'artifact':self.artifacts.lookup_input(payload['chat_id'],payload['request_id'],payload['operation'])}
                elif path == "/messaging/local-command":
                    self.fields(payload, {"chat_id", "request_id", "command"})
                    data = self.deliveries.reserve_command(payload["chat_id"], payload["request_id"], payload["command"])
                elif path == "/messaging/google-control":
                    self.fields(payload, {"chat_id", "request_id", "command_attempt", "text", "keyboard"})
                    self.deliveries.validate_google_menu(payload["text"], payload["keyboard"])
                    data = self.deliveries.complete_command(payload["chat_id"], payload["request_id"],
                                                           payload["command_attempt"], payload["text"], payload["keyboard"])
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
