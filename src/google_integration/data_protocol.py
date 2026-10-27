"""Standard-library-only framing and validation for private Google data traffic."""
import json
import re
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request


class GoogleUnavailable(RuntimeError):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def remaining(deadline):
    duration = deadline - time.monotonic()
    if duration <= 0:
        raise GoogleUnavailable("Google request timed out.")
    return min(10, duration)


def bounded_body(response, limit, deadline):
    body = bytearray()
    while len(body) <= limit:
        if response.fp is None:
            remaining(deadline)
            return body
        # read1 performs at most one socket read; update timeout before every read to bound slow bodies.
        response.fp.raw._sock.settimeout(remaining(deadline))
        chunk = response.read1(min(8192, limit + 1 - len(body)))
        if not chunk:
            remaining(deadline)
            return body
        body.extend(chunk)
    raise ValueError("Google response too large.")


def arguments(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid Gmail arguments.")
    if value.get("operation") == "search_gmail" and set(value) == {"operation", "query", "limit"}:
        if (not isinstance(value["query"], str) or not 1 <= len(value["query"]) <= 1024
                or any(ord(c) < 32 for c in value["query"])
                or type(value["limit"]) is not int or not 1 <= value["limit"] <= 5):
            raise ValueError("Invalid Gmail search.")
    elif value.get("operation") == "read_gmail" and set(value) == {"operation", "message_id"}:
        message_id(value["message_id"])
    else:
        raise ValueError("Unsupported Gmail operation.")
    return value


def message_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid Gmail message ID.")
    return value


def private_post(opener, url, secret, value, deadline, maximum=524288):
    request = Request(url, data=json.dumps(value).encode(), method="POST",
                      headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=remaining(deadline)) as response:
            result = json.loads(bounded_body(response, maximum, deadline))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except HTTPError as error:
        error.close()
        raise GoogleUnavailable("Google data request unavailable.") from None
    except (URLError, OSError, ValueError, HTTPException, RecursionError):
        raise GoogleUnavailable("Google data request unavailable.") from None


def body(handler, maximum=16384):
    if (handler.headers.get("Transfer-Encoding") is not None
            or len(handler.headers.get_all("Content-Length", [])) != 1
            or handler.headers.get_content_type() != "application/json"):
        raise ValueError()
    size = handler.headers["Content-Length"]
    if not size.isascii() or not size.isdigit() or not 0 < int(size) <= maximum:
        raise ValueError()
    value = json.loads(handler.rfile.read(int(size)))
    if not isinstance(value, dict):
        raise ValueError()
    return value


def authorization(handler):
    headers = handler.headers.get_all("Authorization", [])
    if (len(headers) != 1 or not headers[0].startswith("Bearer ")
            or handler.headers.get("Origin") is not None
            or len(handler.headers.get_all("Host", [])) != 1):
        raise PermissionError()
    return headers[0][7:]


def capability(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise PermissionError()
    return value
