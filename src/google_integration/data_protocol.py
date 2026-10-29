"""Standard-library-only framing and validation for private Google data traffic."""
import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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


def calendar_id(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024 or value == "primary"
            or any(not c.isprintable() or c.isspace() for c in value)):
        raise ValueError("Exact calendar ID required.")
    return value


def event_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value):
        raise ValueError("Invalid event ID.")
    return value


def operation_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
        raise ValueError("Stable operation key required (16-128 ASCII characters).")
    return value


def timestamp(value):
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value)):
        raise ValueError("RFC3339 timestamp with explicit offset required.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event_time(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid event time.")
    if set(value) == {"date"}:
        raw = value["date"]
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise ValueError("Invalid all-day date.")
        return date.fromisoformat(raw)
    if set(value) not in ({"dateTime"}, {"dateTime", "timeZone"}):
        raise ValueError("Use either date or dateTime, not both.")
    if "timeZone" in value:
        zone = value["timeZone"]
        if not isinstance(zone, str) or not 1 <= len(zone) <= 128:
            raise ValueError("Invalid timezone.")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Valid IANA timezone required.") from None
    return timestamp(value["dateTime"])


def event_fields(value, *, create=False):
    if (not isinstance(value, dict) or not value
            or not set(value) <= {"summary", "description", "location", "start", "end", "colorId"}):
        raise ValueError("Only summary (title), description, location, start, end and colorId are supported.")
    for key, limit in (("summary", 256), ("description", 4000), ("location", 512)):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > limit
                             or any(unsafe_text_control(c) for c in value[key])):
            raise ValueError("Invalid event text.")
    if "colorId" in value:
        if value["colorId"] is None:
            if create:
                raise ValueError("Omit colorId to use the calendar default when creating an event.")
        else:
            color_id(value["colorId"])
    if create and not {"start", "end"} <= set(value):
        raise ValueError("Start and exclusive end required.")
    for key in ("start", "end"):
        if key in value:
            event_time(value[key])
    if {"start", "end"} <= set(value):
        start, end = event_time(value["start"]), event_time(value["end"])
        if type(start) is not type(end) or end <= start:
            raise ValueError("Matching time forms and end after start required; all-day end is exclusive.")
    return value


def unsafe_text_control(character):
    # Keep joiners, combining marks and directional text; reject control codes and invalid surrogates.
    return character not in "\n\t" and unicodedata.category(character) in {"Cc", "Cs"}


def color_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", value):
        raise ValueError("Use an event color ID returned by list_calendar_event_colors, or null to reset an edit.")
    return value


def page_token(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(not c.isprintable() or c.isspace() for c in value)):
        raise ValueError("Invalid Calendar page token; restart the same bounded search without a token.")
    return value


def calendar_arguments(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid Calendar arguments.")
    operation = value.get("operation")
    required = {
        "list_calendars": {"offset", "limit"},
        "list_calendar_event_colors": {"calendar_id"},
        "list_calendar_events": {"calendar_id", "time_min", "time_max", "limit"},
        "read_calendar_event": {"calendar_id", "event_id"},
        "create_calendar_event": {"calendar_id", "operation_key", "fields"},
        "update_calendar_event": {"calendar_id", "event_id", "etag", "occurrence", "operation_key", "fields"},
        "delete_calendar_event": {"calendar_id", "event_id", "etag", "occurrence", "operation_key"},
        "calendar_operation_status": {"calendar_id", "operation_key"},
    }
    optional = {"query", "page_token"} if operation == "list_calendar_events" else set()
    if (operation not in required or not required[operation] | {"operation"} <= set(value)
            or not set(value) <= required[operation] | optional | {"operation"}):
        raise ValueError("Unsupported Calendar operation or arguments.")
    if "calendar_id" in value:
        calendar_id(value["calendar_id"])
    if "event_id" in value:
        event_id(value["event_id"])
    if "operation_key" in value:
        operation_key(value["operation_key"])
    if "limit" in value and (type(value["limit"]) is not int or not 1 <= value["limit"] <= (20 if operation == "list_calendars" else 5)):
        raise ValueError("Invalid Calendar limit.")
    if "offset" in value and (type(value["offset"]) is not int or not 0 <= value["offset"] <= 500):
        raise ValueError("Invalid Calendar offset.")
    if operation == "list_calendar_events":
        start, end = timestamp(value["time_min"]), timestamp(value["time_max"])
        if not timedelta(0) < end - start <= timedelta(days=93):
            raise ValueError("Explicit event window must be at most 93 days.")
        if "query" in value and (not isinstance(value["query"], str) or not 1 <= len(value["query"]) <= 1024
                                 or any(unsafe_text_control(c) or c in "\n\t" for c in value["query"])):
            raise ValueError("Calendar search requires 1-1024 characters without control codes.")
        if "page_token" in value:
            page_token(value["page_token"])
    if "fields" in value:
        event_fields(value["fields"], create=operation == "create_calendar_event")
    if "etag" in value:
        if not isinstance(value["etag"], str) or not re.fullmatch(r'"[\x21\x23-\x7e]{1,254}"', value["etag"]):
            raise ValueError("Previously read quoted etag required.")
        occurrence = value["occurrence"]
        if occurrence is not None:
            if not isinstance(occurrence, dict) or set(occurrence) != {"recurring_event_id", "original_start_time"}:
                raise ValueError("Explicit single occurrence identity required.")
            event_id(occurrence["recurring_event_id"])
            event_time(occurrence["original_start_time"])
            if occurrence["recurring_event_id"] == value["event_id"]:
                raise ValueError("Series-wide changes unsupported.")
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
