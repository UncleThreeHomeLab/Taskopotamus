"""Channel-neutral reply items; file bytes never enter message manifests."""
import re

from src.profiles.auth import CONSOLE_CHAT_ID, ProfileTokens


def identity(chat_id, request_id):
    ProfileTokens.validate_chat(chat_id)
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9]{1,20}", request_id):
        raise ValueError("A numeric request ID is required.")
    return "console" if chat_id == CONSOLE_CHAT_ID else "telegram"


def opaque_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid identifier.")
    return value


def validate_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        raise ValueError("Invalid reply items.")
    result = []
    text_characters, attachments = 0, 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid reply item.")
        if item.get("type") == "text" and set(item) == {"type", "text"}:
            if not isinstance(item["text"], str) or not item["text"] or len(item["text"]) > 64000:
                raise ValueError("Invalid reply text.")
            item["text"].encode("utf-8")
            text_characters += len(item["text"])
            result.append(dict(item))
        elif item.get("type") in ("image", "file") and set(item) <= {"type", "artifact_id", "caption"}:
            opaque_id(item.get("artifact_id"))
            caption = item.get("caption", "")
            if not isinstance(caption, str) or len(caption) > 16000 or "\x00" in caption:
                raise ValueError("Invalid caption.")
            caption.encode("utf-8")
            attachments += 1
            result.append({"type": item["type"], "artifact_id": item["artifact_id"], "caption": caption})
        else:
            raise ValueError("Invalid reply item.")
    if text_characters > 64000 or attachments > 10:
        raise ValueError("Reply exceeds supported envelope.")
    return result


def delivery_items(items):
    """Conservative Unicode chunks preserve exact text on Telegram."""
    result = []
    for item in validate_items(items):
        if item["type"] == "text":
            result.extend({"type": "text", "text": item["text"][i:i + 2000]}
                          for i in range(0, len(item["text"]), 2000))
        else:
            caption, units, boundary = item["caption"], 0, 0
            for character in caption:
                width = 2 if ord(character) > 0xFFFF else 1
                if units + width > 1024:
                    break
                units += width
                boundary += 1
            result.append({**item, "caption": caption[:boundary]})
            result.extend({"type": "text", "text": caption[i:i + 2000]}
                          for i in range(boundary, len(caption), 2000))
    return result
