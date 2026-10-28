"""Private DATA listener and fixed trusted run-check callback; no control actions."""
import json
import secrets
import time
from urllib.request import ProxyHandler, build_opener

from .gmail import Gmail
from .calendar import Calendar
from .data_protocol import NoRedirects, remaining, private_post, arguments, calendar_arguments, authorization, body, capability


class GoogleData:
    def __init__(self, oauth, secret, gmail=None, calendar=None):
        self.oauth, self.secret = oauth, secret
        self.gmail = gmail or Gmail()
        self.calendar = calendar or Calendar(oauth.store)
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def execute(self, value, service="gmail"):
        if set(value) != {"capability", "arguments"}:
            raise ValueError()
        token = capability(value["capability"])
        operation = (calendar_arguments if service == "calendar" else arguments)(value["arguments"])
        deadline, expected = time.monotonic() + 20, None

        def guard():
            nonlocal deadline, expected
            started = time.monotonic()
            checked = private_post(self.opener, "http://agent:8080/google/run-check", self.secret,
                                   {"capability": token}, deadline, 4096)
            if (set(checked) != {"chat_id", "request_id", "remaining_seconds"}
                    or not isinstance(checked["chat_id"], str) or not isinstance(checked["request_id"], str)
                    or type(checked["remaining_seconds"]) not in (float, int)
                    or not 0 < checked["remaining_seconds"] <= 1800):
                raise PermissionError()
            identity = checked["chat_id"], checked["request_id"]
            if expected is not None and identity != expected:
                raise PermissionError()
            expected = identity
            deadline = min(deadline, started + checked["remaining_seconds"])
            remaining(deadline)
            return deadline

        guard()
        if not self.oauth.config.enabled:
            raise PermissionError()
        if service == "calendar":
            kind = ("calendar_list" if operation["operation"] == "list_calendars" else
                    "calendar_edit" if operation["operation"] in ("create_calendar_event", "update_calendar_event", "delete_calendar_event") else "calendar_read")
            return self.oauth.permissions.dispatch(
                expected[0], kind,
                lambda access, _calendar, end, context: self.calendar.execute(
                    operation, access, end, context["guard"], chat=expected[0], request_id=expected[1], context=context),
                calendar_id=operation.get("calendar_id"), run_guard=guard, deadline=deadline, include_context=True)
        return self.oauth.permissions.dispatch(
            expected[0], "gmail_read",
            lambda access, _calendar, end: self.gmail.execute(operation, access, end, guard),
            run_guard=guard, deadline=deadline,
        )


def make_data_handler(oauth, secret, gmail=None, calendar=None):
    from .server import make_handler

    base = make_handler(oauth, secret)
    service = GoogleData(oauth, secret, gmail, calendar)

    class Handler(base):
        def respond(self):
            try:
                credential = authorization(self)
                if (self.headers["Host"] not in ("google:8082", "localhost:8082", "127.0.0.1:8082")
                        or not secrets.compare_digest(credential.encode(), secret.encode())):
                    raise PermissionError()
                if self.command != "POST" or self.path not in ("/data/gmail", "/data/calendar"):
                    return self.send(404, {"error": "Not found"})
                value = body(self, 131072 if self.path == "/data/calendar" else 16384)
                self.deadline.cancel()
                result = service.execute(value, "calendar" if self.path == "/data/calendar" else "gmail")
                if len(json.dumps(result).encode()) > 524288:
                    raise ValueError()
                return self.send(200, result)
            except PermissionError:
                self.send(403, {"error": "Google data access denied."})
            except (ValueError, TypeError, UnicodeError, RecursionError):
                self.send(400, {"error": "Invalid Google data request."})
            except Exception:
                self.send(503, {"error": "Google data unavailable."})

    return Handler
