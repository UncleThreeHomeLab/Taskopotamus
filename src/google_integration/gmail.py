"""Bounded read-only Gmail transport. Provider strings remain untrusted data."""
import base64
import binascii
import json
import re
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .data_protocol import GoogleUnavailable, NoRedirects, bounded_body, remaining, arguments, message_id


GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
HEADERS = ("From", "To", "Subject", "Date")


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden, self.text = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden.append(tag)
        elif tag in ("br", "p", "div", "li") and not self.hidden:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def text(value, limit):
    if not isinstance(value, str):
        raise ValueError("Invalid Gmail text.")
    return "".join(c for c in value[:limit] if c.isprintable() or c in "\n\t")


def decode_text(raw, charset, result):
    try:
        return raw.decode(charset or "utf-8", errors="strict")
    except (UnicodeError, LookupError):
        result["incomplete"] = True
        return raw.decode("utf-8", errors="replace")


def summarize(value, include_body=False):
    if not isinstance(value, dict):
        raise ValueError("Invalid Gmail message.")
    result = {"message_id": message_id(value.get("id")), "headers": {},
              "snippet": text(value.get("snippet", ""), 512),
              "truncated": len(value.get("snippet", "")) > 512, "incomplete": False}
    payload = value.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("Invalid Gmail payload.")
    headers = payload.get("headers", [])
    if not isinstance(headers, list) or len(headers) > 100:
        raise ValueError("Invalid Gmail headers.")
    for header in headers:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str):
            raise ValueError("Invalid Gmail header.")
        name = next((name for name in HEADERS if name.lower() == header["name"].lower()), None)
        if name:
            raw = header.get("value")
            if not isinstance(raw, str):
                raise ValueError("Invalid Gmail header.")
            decoded = "".join(decode_text(piece, charset, result) if isinstance(piece, bytes) else piece
                              for piece, charset in decode_header(raw[:4096]))
            result["headers"][name] = text(decoded, 1024)
            result["truncated"] |= len(raw) > 1024
    if not include_body:
        return result
    plain, html, nodes = [], [], 0

    def visit(part, depth):
        nonlocal nodes
        nodes += 1
        if depth > 10 or nodes > 100:
            result["incomplete"] = True
            return
        if not isinstance(part, dict):
            raise ValueError("Invalid Gmail MIME part.")
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if not isinstance(mime, str) or not isinstance(body, dict):
            raise ValueError("Invalid Gmail MIME body.")
        part_headers = part.get("headers", [])
        if not isinstance(part_headers, list) or len(part_headers) > 100:
            raise ValueError("Invalid Gmail MIME headers.")
        attachment = bool(part.get("filename") or body.get("attachmentId") or mime.lower().startswith("message/"))
        charset = "utf-8"
        for header in part_headers:
            if not isinstance(header, dict) or not isinstance(header.get("name"), str) or not isinstance(header.get("value"), str):
                raise ValueError("Invalid Gmail MIME header.")
            if header["name"].lower() == "content-disposition" and header["value"].lower().lstrip().startswith("attachment"):
                attachment = True
            if header["name"].lower() in ("content-type", "content-disposition"):
                mime_header = Message()
                mime_header[header["name"].lower()] = header["value"]
                attachment |= mime_header.get_filename() is not None
                if header["name"].lower() == "content-type":
                    charset = mime_header.get_content_charset() or "utf-8"
        if attachment:
            result["incomplete"] = True
            return
        if mime.lower() in ("text/plain", "text/html"):
            data = body.get("data", "")
            if not isinstance(data, str) or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data):
                raise ValueError("Invalid Gmail body encoding.")
            try:
                decoded = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
                content = decode_text(decoded, charset, result)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid Gmail body encoding.") from None
            if type(body.get("size", 0)) is not int or body.get("size", 0) < 0:
                raise ValueError("Invalid Gmail body size.")
            result["incomplete"] |= body.get("size", 0) > len(decoded)
            (plain if mime.lower() == "text/plain" else html).append(content)
        elif not mime.lower().startswith("multipart/"):
            result["incomplete"] = True
        parts = part.get("parts", [])
        if not isinstance(parts, list):
            raise ValueError("Invalid Gmail MIME parts.")
        for child in parts:
            if nodes >= 100:
                result["incomplete"] = True
                break
            visit(child, depth + 1)

    visit(payload, 0)
    content = "\n".join(plain)
    if not plain and html:
        parser = PlainHTML()
        parser.feed("\n".join(html))
        parser.close()
        content = "".join(parser.text)
    result["body"] = text(content, 16000)
    result["truncated"] |= len(content) > 16000
    result["incomplete"] |= not plain and not html
    return result


class Gmail:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def get(self, identifier, parameters, token, deadline, guard):
        deadline = min(deadline, guard())
        url = GMAIL_URL + ("/" + quote(message_id(identifier), safe="") if identifier is not None else "")
        request = Request(url + "?" + urlencode(parameters, doseq=True), method="GET",
                          headers={"Authorization": "Bearer " + token})
        try:
            with self.opener.open(request, timeout=remaining(deadline)) as response:
                value = json.loads(bounded_body(response, 512 * 1024, deadline))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except HTTPError as error:
            error.close()
            raise GoogleUnavailable("Gmail unavailable.") from None
        except (URLError, OSError, ValueError, HTTPException, RecursionError):
            raise GoogleUnavailable("Gmail unavailable.") from None

    def execute(self, value, token, deadline, guard):
        arguments(value)
        if value["operation"] == "read_gmail":
            result = self.get(value["message_id"], {"format": "full", "fields": "id,snippet,payload"}, token, deadline, guard)
            if result.get("id") != value["message_id"]:
                raise GoogleUnavailable("Gmail response invalid.")
            return {"untrusted_email_data": summarize(result, True)}
        found = self.get(None, {"q": value["query"], "maxResults": value["limit"],
                               "fields": "messages(id),nextPageToken"}, token, deadline, guard)
        messages = found.get("messages", [])
        if not isinstance(messages, list) or len(messages) > value["limit"]:
            raise GoogleUnavailable("Gmail response invalid.")
        results, seen = [], set()
        for entry in messages:
            identifier = message_id(entry.get("id") if isinstance(entry, dict) else None)
            if identifier in seen:
                raise GoogleUnavailable("Gmail response invalid.")
            seen.add(identifier)
            result = self.get(identifier, {"format": "metadata", "metadataHeaders": HEADERS,
                                          "fields": "id,snippet,payload(headers)"}, token, deadline, guard)
            if result.get("id") != identifier:
                raise GoogleUnavailable("Gmail response invalid.")
            results.append(summarize(result))
        return {"untrusted_email_data": results, "incomplete": bool(found.get("nextPageToken"))}
