"""Publish immutable worker messages into the current request's durable outbox."""
import os
import re

from .artifacts import operation_id
from .image_validation import validate_image
from .messages import validate_items


class MessagePublication:
    def __init__(self, artifacts, deliveries):
        self.artifacts, self.deliveries = artifacts, deliveries

    @staticmethod
    def key(message_id, request_signature):
        operation_id(message_id)
        if not isinstance(request_signature, str) or not re.fullmatch(r"[a-f0-9]{64}", request_signature):
            raise ValueError("Invalid message signature.")
        return "worker_" + message_id

    def lookup(self, capability, message_id, request_signature):
        key = self.key(message_id, request_signature)
        with self.artifacts.database.connect() as db:
            chat, request = self.artifacts.authorize(db, capability)
            return self.deliveries.message_lookup(db, chat, request, key, request_signature)

    def send(self, capability, message_id, request_signature, items):
        key = self.key(message_id, request_signature)
        items = validate_items(items)
        previous = self.lookup(capability, message_id, request_signature)
        if previous is not None:
            if previous["items"] != items:
                raise ValueError("Message identity already used for different content.")
            return previous
        chat, request = self.artifacts.scope(capability)
        images = {}
        # Immutable private snapshots can be decoded before taking publication locks.
        for item in items:
            if item["type"] == "text":
                continue
            info = self.artifacts.describe(item["artifact_id"], chat, request)
            if info["direction"] != "output":
                raise PermissionError("Output attachment required.")
            if item["type"] == "image":
                if info["size"] > self.artifacts.policy.image_max_bytes:
                    raise ValueError("Unsupported image size.")
                descriptor = os.open(self.artifacts.root / info["artifact_id"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as source:
                    images[info["artifact_id"]] = validate_image(source, info["mime"])
        with self.artifacts.database.connect() as db:
            if self.artifacts.authorize(db, capability) != (chat, request):
                raise PermissionError()
            self.deliveries._lock(db, chat, request)
            self.artifacts.authorize(db, capability)
            previous = self.deliveries.message_lookup(db, chat, request, key, request_signature)
            if previous is not None:
                if previous["items"] != items:
                    raise ValueError("Message identity already used for different content.")
                return previous
            self.artifacts.commit_outputs(db, chat, request, items, images)
            identifier = self.deliveries.append(db, chat, request, key, items, request_signature)
            # SQL locks can wait too: reject the complete transaction if its authority expired meanwhile.
            self.artifacts.authorize(db, capability)
            return {"state": "queued", "delivery_id": identifier, "items": items}
