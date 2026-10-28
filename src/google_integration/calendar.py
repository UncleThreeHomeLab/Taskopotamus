"""Bounded Calendar operations and durable encrypted replay protection."""
import hashlib
import json
import secrets
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .data_protocol import (GoogleUnavailable, NoRedirects, bounded_body, calendar_arguments,
                            event_fields, event_id, event_time, remaining)
from .permissions import CALENDAR_EDIT, CALENDAR_READ, scopes_allow


CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/"
EVENT_FIELDS = "id,etag,eventType,status,summary,description,location,start,end,recurrence,recurringEventId,originalStartTime,extendedProperties/private"
MARKER = "taskopotamusOperation"


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def occurrence(value):
    if "recurringEventId" not in value and "originalStartTime" not in value:
        return None
    parent = event_id(value.get("recurringEventId"))
    original = value.get("originalStartTime")
    event_time(original)
    if parent == value.get("id"):
        raise ValueError("Invalid recurring occurrence.")
    return {"recurring_event_id": parent, "original_start_time": original}


def summarize(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid Calendar event.")
    result = {"event_id": event_id(value.get("id")), "etag": value.get("etag"),
              "event_type": value.get("eventType", "default"), "status": value.get("status", "confirmed"),
              "recurring_master": bool(value.get("recurrence")), "occurrence": occurrence(value), "truncated": False}
    if not isinstance(result["etag"], str) or len(result["etag"]) > 256:
        raise ValueError("Invalid Calendar etag.")
    if result["event_type"] not in ("default", "birthday", "focusTime", "fromGmail", "outOfOffice", "workingLocation"):
        raise ValueError("Invalid Calendar event type.")
    if result["status"] not in ("confirmed", "tentative", "cancelled"):
        raise ValueError("Invalid Calendar event status.")
    for key, limit in (("summary", 256), ("description", 4000), ("location", 512)):
        raw = value.get(key, "")
        if not isinstance(raw, str):
            raise ValueError("Invalid Calendar text.")
        result[key] = "".join(c for c in raw[:limit] if c.isprintable() or c in "\n\t")
        result["truncated"] |= len(raw) > limit
    for key in ("start", "end"):
        if key in value:
            event_time(value[key])
            result[key] = value[key]
    return result


class CalendarHTTP(GoogleUnavailable):
    def __init__(self, status):
        self.status = status
        super().__init__("Calendar provider unavailable.")


class Calendar:
    def __init__(self, store):
        self.store = store
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, method, calendar, identifier, token, deadline, guard, *, parameters=None, fields=None, etag=None):
        deadline = min(deadline, guard())
        url = CALENDAR_URL + quote(calendar, safe="") + "/events"
        if identifier is not None:
            url += "/" + quote(event_id(identifier), safe="")
        parameters = {**(parameters or {}), "fields": ("items(" + EVENT_FIELDS + "),nextPageToken" if identifier is None and method == "GET" else EVENT_FIELDS)}
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if etag is not None:
            headers["If-Match"] = etag
        request = Request(url + "?" + urlencode(parameters), method=method, headers=headers,
                          data=None if fields is None else json.dumps(fields, ensure_ascii=False).encode())
        try:
            with self.opener.open(request, timeout=remaining(deadline)) as response:
                raw = bounded_body(response, 512 * 1024, deadline)
                if method == "DELETE" and response.status == 204 and not raw:
                    return None
                value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except HTTPError as error:
            status = error.code
            error.close()
            raise CalendarHTTP(status) from None
        except (URLError, OSError, ValueError, HTTPException, RecursionError):
            raise GoogleUnavailable("Calendar provider unavailable.") from None

    def journal(self, chat, key):
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM calendar_operations WHERE chat=? AND operation_key=?", (chat, key)).fetchone()
            return self.store.open(chat, 0, "calendar-operation:" + key, row["payload"]) if row else None

    def save(self, chat, key, value, *, expected_status="uncertain"):
        # Immutable encryption context survives disconnects and same-account reconnections.
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM calendar_operations WHERE chat=? AND operation_key=?", (chat, key)).fetchone()
            current = self.store.open(chat, 0, "calendar-operation:" + key, row["payload"])
            if current["binding"] != value["binding"] or current["status"] != expected_status:
                raise GoogleUnavailable("Calendar operation state changed; inspect its reference.")
            db.execute("UPDATE calendar_operations SET payload=? WHERE chat=? AND operation_key=?",
                       (self.store.seal(chat, 0, "calendar-operation:" + key, value), chat, key))

    def reserve(self, chat, key, value):
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO calendar_operations VALUES (?,?,?)",
                       (chat, key, self.store.seal(chat, 0, "calendar-operation:" + key, value)))
            row = db.execute("SELECT payload FROM calendar_operations WHERE chat=? AND operation_key=?", (chat, key)).fetchone()
            return self.store.open(chat, 0, "calendar-operation:" + key, row["payload"])

    def attempt(self, chat, key, binding):
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM calendar_operations WHERE chat=? AND operation_key=?", (chat, key)).fetchone()
            saved = self.store.open(chat, 0, "calendar-operation:" + key, row["payload"])
            if saved["binding"] != binding:
                raise ValueError("Operation reference arguments changed.")
            if saved["status"] != "prepared":
                return saved, False
            saved["status"] = "uncertain"
            db.execute("UPDATE calendar_operations SET payload=? WHERE chat=? AND operation_key=?",
                       (self.store.seal(chat, 0, "calendar-operation:" + key, saved), chat, key))
            return saved, True

    @staticmethod
    def status(key, saved):
        return {"operation_reference": key, "status": saved["status"], "event_id": saved["event_id"], **saved.get("result", {})}

    def execute(self, value, token, deadline, guard, *, chat, request_id, context):
        calendar_arguments(value)
        operation = value["operation"]
        if operation == "list_calendars":
            entries = [{"calendar_id": key, "title": item["title"], "mode": item["mode"]}
                       for key, item in context["calendars"].items()
                       if item["mode"] in ("read", "edit") and scopes_allow(
                           {CALENDAR_EDIT if item["mode"] == "edit" else CALENDAR_READ}, context["scopes"])]
            start, limit = value["offset"], value["limit"]
            result = {"untrusted_calendar_data": entries[start:start + limit], "incomplete": start + limit < len(entries)}
        elif operation in ("list_calendar_events", "read_calendar_event"):
            identifier = value.get("event_id")
            parameters = ({"timeMin": value["time_min"], "timeMax": value["time_max"], "maxResults": value["limit"],
                           "singleEvents": "true", "orderBy": "startTime"} if identifier is None else {})
            response = self.request("GET", value["calendar_id"], identifier, token, deadline, guard, parameters=parameters)
            if identifier is not None:
                if response.get("id") != identifier:
                    raise GoogleUnavailable("Calendar response invalid.")
                result = {"untrusted_calendar_data": summarize(response)}
            else:
                entries = response.get("items", [])
                if not isinstance(entries, list) or len(entries) > value["limit"]:
                    raise GoogleUnavailable("Calendar response invalid.")
                result = {"untrusted_calendar_data": [summarize(item) for item in entries],
                          "incomplete": bool(response.get("nextPageToken"))}
        else:
            result = self.mutate(value, token, deadline, guard, chat, request_id, context)
        guard()
        return result

    def mutate(self, value, token, deadline, guard, chat, request_id, context):
        operation, key, calendar = value["operation"], value["operation_key"], value["calendar_id"]
        binding = canonical_hash({"account": context["sub"], "arguments": value})
        saved = self.journal(chat, key)
        if saved is None and operation != "calendar_operation_status":
            saved = self.reserve(chat, key, {
                "account": canonical_hash(context["sub"]), "calendar": canonical_hash(calendar), "binding": binding,
                "request_id": request_id, "status": "prepared", "event_id": value.get("event_id") or secrets.token_hex(16)})
        if saved is not None:
            if saved["account"] != canonical_hash(context["sub"]) or saved["calendar"] != canonical_hash(calendar):
                raise PermissionError("Operation belongs to another account or calendar.")
            if operation == "calendar_operation_status":
                return self.status(key, saved)
            if saved["binding"] != binding:
                raise ValueError("Operation reference already binds different arguments. Do not replace uncertain operations with new keys.")
            if saved["status"] != "prepared":
                return self.status(key, saved)
        elif operation == "calendar_operation_status":
            raise ValueError("Unknown operation reference.")
        identifier = saved["event_id"]
        if operation == "create_calendar_event":
            marker = canonical_hash({"account": context["sub"], "calendar": calendar, "operation": key, "binding": binding})
            fields = {**value["fields"], "id": identifier, "extendedProperties": {"private": {MARKER: marker}}}
            method, target = "POST", None
        else:
            before = self.request("GET", calendar, identifier, token, deadline, guard)
            if before.get("id") != identifier:
                raise GoogleUnavailable("Calendar preflight response invalid.")

            def reject(reason):
                saved.update(status="rejected", result={"reason": reason})
                self.save(chat, key, saved, expected_status="prepared")
                return self.status(key, saved)

            if before.get("etag") != value["etag"]:
                return reject("Event changed; read fresh etag and reconsider before creating a new operation.")
            if before.get("eventType", "default") != "default" or "recurrence" in before:
                return reject("Unsupported event: recurring masters and special event types cannot be changed.")
            if before.get("status") == "cancelled" or occurrence(before) != value["occurrence"]:
                return reject("Event cancelled or single occurrence identity differs; read again and reconsider.")
            if operation == "update_calendar_event":
                combined = {name: before[name] for name in ("start", "end") if name in before}
                combined.update({name: value["fields"][name] for name in ("start", "end") if name in value["fields"]})
                try:
                    event_fields(combined, create=True)
                except ValueError:
                    return reject("Merged start/end invalid; use matching time forms and an exclusive end after start.")
                method, fields = "PATCH", value["fields"]
            else:
                method = "PATCH" if value["occurrence"] is not None else "DELETE"
                fields = {"status": "cancelled"} if method == "PATCH" else None
            target = identifier
        guard()
        # Commit before sending: every failure after this point requires inspection, never automatic replay.
        saved, claimed = self.attempt(chat, key, binding)
        if not claimed:
            return self.status(key, saved)
        try:
            response = self.request(method, calendar, target, token, deadline, guard,
                                    fields=fields, etag=value.get("etag"))
        except CalendarHTTP as error:
            if operation == "create_calendar_event" and error.status == 409:
                response = self.request("GET", calendar, identifier, token, deadline, guard)
            elif error.status == 412:
                saved.update(status="rejected", result={"reason": "Stale etag; read again and reconsider."})
                self.save(chat, key, saved)
                return self.status(key, saved)
            else:
                raise
        if response is not None:
            if response.get("id") != identifier:
                raise GoogleUnavailable("Calendar write outcome uncertain; inspect operation reference.")
            if operation == "create_calendar_event" and response.get("extendedProperties", {}).get("private", {}).get(MARKER) != marker:
                raise GoogleUnavailable("Calendar create ownership unproven; inspect operation reference.")
            if response.get("eventType", "default") != "default" or "recurrence" in response:
                raise GoogleUnavailable("Calendar write response unsupported; inspect operation reference.")
            if operation == "delete_calendar_event" and value["occurrence"] is not None and response.get("status") != "cancelled":
                raise GoogleUnavailable("Calendar cancellation outcome uncertain; inspect operation reference.")
            identity = occurrence(response)
            if operation != "create_calendar_event" and identity != value["occurrence"]:
                raise GoogleUnavailable("Calendar occurrence outcome uncertain.")
            if operation == "delete_calendar_event":
                result = {"event_id": identifier, "deleted": True, "occurrence": identity}
            else:
                data = summarize(response)
                if operation == "create_calendar_event" and identity is not None:
                    raise GoogleUnavailable("Calendar create response is recurring; inspect operation reference.")
                result = {"event_id": identifier, "etag": data["etag"], "occurrence": identity}
        elif method == "DELETE":
            result = {"event_id": identifier, "deleted": True}
        else:
            raise GoogleUnavailable("Calendar write outcome uncertain.")
        saved.update(status="confirmed", result=result)
        self.save(chat, key, saved)
        return self.status(key, saved)
