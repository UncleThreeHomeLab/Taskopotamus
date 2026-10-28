"""Synthetic offline Calendar validation, provider bridge and durable mutation checks."""
import asyncio
import copy
import http.client
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import Mock, patch

import test_google_permissions as permissions_tests
from database_support import isolated_database
from src.messaging.artifacts import ArtifactPolicy, ArtifactStore
from src.agent.google_data import GoogleDataAPI
from src.agent.google_tools import google_config
from src.agent.http_server import AgentHTTPServer
from src.agent.server import make_handler
from src.google_integration.calendar import Calendar, CalendarHTTP, MARKER
from src.google_integration.data_api import make_data_handler
from src.google_integration.data_protocol import calendar_arguments, NoRedirects
from src.google_integration.store import GoogleStore
from src.worker.calendar_mcp import CalendarClient, make_server


CALENDAR = "calendar-exact@example.test"
DATA_SECRET = "synthetic-data-secret-calendar-tests-000000000"
FIELDS = {"summary": "Synthetic event", "start": {"dateTime": "2026-10-28T10:00:00+00:00", "timeZone": "Europe/London"},
          "end": {"dateTime": "2026-10-28T11:00:00+00:00", "timeZone": "Europe/London"}}
CREATE = {"operation": "create_calendar_event", "calendar_id": CALENDAR, "operation_key": "synthetic-create-0001", "fields": FIELDS}


def event(identifier="event_1", **extra):
    return {"id": identifier, "etag": '"v1"', "status": "confirmed", "eventType": "default", **copy.deepcopy(FIELDS), **extra}


class Routes:
    def __init__(self, routes):
        self.routes = routes
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        if parsed.netloc not in self.routes:
            raise AssertionError("Non-test network access")
        headers = dict(request.header_items())
        headers["Host"] = parsed.netloc
        return self.opener.open(Request(f"http://127.0.0.1:{self.routes[parsed.netloc]}{parsed.path}" +
                                        ("?" + parsed.query if parsed.query else ""),
                                        data=request.data, method=request.method, headers=headers), timeout=timeout)


class CalendarValidationTests(unittest.TestCase):
    def test_rejects_untrusted_fields_ambiguity_and_unbounded_inputs(self):
        calendar_arguments(copy.deepcopy(CREATE))
        all_day = {**CREATE, "fields": {"start": {"date": "2026-10-28"}, "end": {"date": "2026-10-29"}}}
        calendar_arguments(all_day)
        bad = [{**CREATE, "calendar_id": "primary"}, {**CREATE, "chat_id": "43"}, {**CREATE, "operation_key": "short"},
               {**CREATE, "fields": {**FIELDS, "attendees": []}}, {**CREATE, "fields": {**FIELDS, "recurrence": []}},
               {**CREATE, "fields": {**FIELDS, "reminders": {}}}, {**CREATE, "fields": {**FIELDS, "summary": "x" * 257}},
               {**CREATE, "fields": {**FIELDS, "start": {"dateTime": "2026-10-28T10:00:00"}}},
               {**CREATE, "fields": {**FIELDS, "start": {"dateTime": "2026-10-28T10:00:00Z", "timeZone": "Bad/Zone"}}},
               {**CREATE, "fields": {**FIELDS, "end": FIELDS["start"]}},
               {**all_day, "fields": {"start": {"date": "2026-10-28"}, "end": {"date": "2026-10-28"}}},
               {**CREATE, "fields": {**FIELDS, "start": {"date": "2026-10-28", "dateTime": "2026-10-28T10:00:00Z"}}}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                calendar_arguments(value)

    def test_static_mcp_has_no_provider_credentials_and_extra_arguments_fail(self):
        config = google_config()
        self.assertEqual(config["mcp_servers.calendar.env_vars"], ["TASKOPOTAMUS_REQUEST_SCOPE", "TASKOPOTAMUS_ARTIFACT_AUTH", "TASKOPOTAMUS_AGENT_IP"])
        client = CalendarClient("x" * 43, "10.203.17.30")
        self.assertEqual(client.url, "http://10.203.17.30:8080/google/worker/calendar")
        client.execute = Mock(return_value={"ok": True})
        server = make_server(client)
        async def check():
            tools = await server.list_tools()
            self.assertEqual(len(tools), 7)
            with self.assertRaises(Exception):
                await server.call_tool("create_calendar_event", {**CREATE, "operation": "override"})
        asyncio.run(check())
        client.execute.assert_not_called()


class CalendarBridgeTests(unittest.TestCase):
    control = permissions_tests.GooglePermissionTests.control
    start = permissions_tests.GooglePermissionTests.start
    finish = permissions_tests.GooglePermissionTests.finish
    menu = permissions_tests.GooglePermissionTests.menu
    click = permissions_tests.GooglePermissionTests.click
    settings = permissions_tests.GooglePermissionTests.settings
    all_scopes = permissions_tests.GooglePermissionTests.all_scopes
    select_mode = permissions_tests.GooglePermissionTests.select_mode

    def setUp(self):
        permissions_tests.GooglePermissionTests.setUp(self)
        self.all_scopes()
        self.finish()
        self.click(self.select_mode(self.menu(True), 1, "Edit"), "Save")
        self.database = isolated_database(self)
        self.artifacts = ArtifactStore(self.database, self.root / "artifacts", ArtifactPolicy(), clock=lambda: self.now)
        self.cap = self.artifacts.begin_run("42", "1", 60)
        self.gateway = GoogleDataAPI(self.artifacts, DATA_SECRET)
        self.calendar = Calendar(self.store)
        self.events, self.calls, self.on_effect, self.force_status = {"event_1": event()}, [], None, None
        self.expected_calendar = CALENDAR
        fixture = self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self):
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                prefix = "/calendar/v3/calendars/" + fixture.expected_calendar + "/events"
                self.assert_path = path.startswith(prefix)
                if not self.assert_path:
                    raise AssertionError(path)
                identifier = path[len(prefix):].removeprefix("/") or None
                size = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(size)) if size else None
                fixture.calls.append((self.command, identifier, parse_qs(parts.query), body, self.headers.get("If-Match")))
                status = fixture.force_status or 200
                if fixture.force_status:
                    result = {"error": "synthetic"}
                elif self.command == "GET":
                    result = copy.deepcopy(fixture.events.get(identifier)) if identifier else {"items": list(copy.deepcopy(fixture.events).values())[:5], "nextPageToken": "more"}
                    if result is None:
                        status, result = 404, {}
                elif self.command == "POST":
                    identifier = body["id"]
                    if identifier in fixture.events:
                        status, result = 409, {}
                    else:
                        result = event(identifier, **{k: v for k, v in body.items() if k != "id"})
                        fixture.events[identifier] = copy.deepcopy(result)
                elif self.headers.get("If-Match") != fixture.events[identifier]["etag"]:
                    status, result = 412, {}
                elif self.command == "PATCH":
                    fixture.events[identifier].update(body)
                    fixture.events[identifier]["etag"] = '"v2"'
                    result = copy.deepcopy(fixture.events[identifier])
                    if body == {"status": "cancelled"}:
                        result = {key: result[key] for key in ("id", "status", "recurringEventId", "originalStartTime")}
                else:
                    del fixture.events[identifier]
                    status, result = 204, None
                if self.command != "GET" and fixture.on_effect:
                    fixture.on_effect()
                encoded = b"" if result is None else json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = do_POST = do_PATCH = do_DELETE = respond
        self.provider = self.server(Provider)
        self.agent = self.server(make_handler(Mock(), "synthetic-agent-secret-calendar-00000", google_data=self.gateway))
        self.routes = {"agent:8080": self.agent.server_port, "www.googleapis.com": self.provider.server_port}
        routing = Routes(self.routes)
        self.gateway.opener = self.calendar.opener = routing
        with patch("src.google_integration.data_api.build_opener", return_value=routing):
            self.data = self.server(make_data_handler(self.oauth, DATA_SECRET, calendar=self.calendar))
        self.routes["google:8082"] = self.data.server_port

    def server(self, handler):
        server = AgentHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post(self, value, cap=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.agent.server_port, timeout=10)
        try:
            connection.request("POST", "/google/worker/calendar", json.dumps(value), headers={
                "Host": "agent:8080", "Content-Type": "application/json", "Authorization": "Bearer " + (cap or self.cap)})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def change(self, operation="update_calendar_event", identifier="event_1", key="synthetic-update-0001", **extra):
        value = {"operation": operation, "calendar_id": CALENDAR, "event_id": identifier, "etag": '"v1"',
                 "operation_key": key, "occurrence": None, **extra}
        if operation == "update_calendar_event":
            value.setdefault("fields", {"summary": "Changed"})
        return value

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def test_real_bridge_crud_conditional_patch_preserves_fields_and_empty_delete(self):
        self.assertEqual(self.post({"operation": "read_calendar_event", "calendar_id": CALENDAR, "event_id": "event_1"})[0], 200)
        status, created = self.post(copy.deepcopy(CREATE))
        self.assertEqual(status, 200, created)
        self.assertEqual(created["status"], "confirmed")
        identifier = created["event_id"]
        self.assertRegex(identifier, r"^[0-9a-v]{32}$")
        self.assertIn(MARKER, self.events[identifier]["extendedProperties"]["private"])
        self.events["event_1"]["attendees"] = [{"email": "synthetic@example.test"}]
        self.events["event_1"]["description"] = "x" * 5000
        self.assertEqual(self.post(self.change())[1]["status"], "confirmed")
        self.assertEqual(self.events["event_1"]["attendees"], [{"email": "synthetic@example.test"}])
        self.assertEqual(self.events["event_1"]["description"], "x" * 5000)
        self.assertEqual(self.writes()[-1][0], "PATCH")
        self.assertEqual(self.writes()[-1][3], {"summary": "Changed"})
        self.assertEqual(self.writes()[-1][4], '"v1"')
        deleted = self.post(self.change("delete_calendar_event", etag='"v2"', key="synthetic-delete-0001"))[1]
        self.assertEqual(deleted["status"], "confirmed")
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.writes()[-1][0], "DELETE")

    def test_list_effective_permissions_no_off_metadata_and_bounded_window(self):
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            revision, settings = self.permissions.read(db, "42", row["generation"])
            settings["calendars"]["secret-off@example.test"] = {"title": "hidden", "mode": "off", "role": "owner"}
            self.permissions.write(db, "42", row["generation"], revision + 1, settings)
        status, data = self.post({"operation": "list_calendars", "offset": 0, "limit": 20})
        self.assertEqual(status, 200)
        self.assertNotIn("hidden", str(data))
        self.assertNotIn("secret-off", str(data))
        self.assertEqual(self.calls, [])
        query = {"operation": "list_calendar_events", "calendar_id": CALENDAR, "time_min": "2026-10-28T00:00:00Z",
                 "time_max": "2026-10-29T00:00:00Z", "limit": 5}
        self.assertTrue(self.post(query)[1]["incomplete"])
        self.assertEqual(self.calls[-1][2]["singleEvents"], ["true"])
        self.assertEqual(self.calls[-1][2]["orderBy"], ["startTime"])
        self.assertEqual(self.post({**query, "time_max": "2027-10-29T00:00:00Z"})[0], 400)

    def test_off_read_unknown_removed_and_missing_scopes_deny_writes(self):
        self.click(self.select_mode(self.menu(True), 1, "Read"), "Save")
        self.assertGreaterEqual(self.post(CREATE)[0], 400)
        self.assertEqual(self.post({"operation": "read_calendar_event", "calendar_id": CALENDAR, "event_id": "event_1"})[0], 200)
        self.click(self.select_mode(self.menu(True), 1, "Off"), "Save")
        self.assertEqual(self.post({"operation": "list_calendars", "offset": 0, "limit": 20})[1]["untrusted_calendar_data"], [])
        self.assertGreaterEqual(self.post(CREATE)[0], 400)
        self.assertGreaterEqual(self.post({**CREATE, "calendar_id": "unknown@example.test"})[0], 400)
        self.assertEqual(self.writes(), [])

    def test_single_instance_update_and_cancel_never_touch_master_or_siblings(self):
        original = {"dateTime": "2026-10-28T10:00:00Z"}
        selected = {"recurring_event_id": "master_1", "original_start_time": original}
        self.events.update({"master_1": event("master_1", recurrence=["RRULE:FREQ=DAILY"]),
                            "instance_1": event("instance_1", recurringEventId="master_1", originalStartTime=original),
                            "instance_2": event("instance_2", recurringEventId="master_1", originalStartTime={"dateTime": "2026-10-29T10:00:00Z"})})
        master, sibling = copy.deepcopy(self.events["master_1"]), copy.deepcopy(self.events["instance_2"])
        self.assertEqual(self.post(self.change(identifier="instance_1", occurrence=selected))[1]["status"], "confirmed")
        self.assertEqual(self.post(self.change("delete_calendar_event", "instance_1", "synthetic-instance-cancel", etag='"v2"', occurrence=selected))[1]["status"], "confirmed")
        self.assertEqual(self.writes()[-1][0:2], ("PATCH", "instance_1"))
        self.assertEqual(self.writes()[-1][3], {"status": "cancelled"})
        self.assertEqual(self.events["master_1"], master)
        self.assertEqual(self.events["instance_2"], sibling)
        for identifier, overrides in (("master_1", {}), ("instance_2", {}), ("event_1", {"eventType": "outOfOffice"})):
            self.events[identifier].update(overrides)
            self.assertEqual(self.post(self.change(identifier=identifier, key="reject-" + identifier + "-0001"))[1]["status"], "rejected")
        self.assertEqual(len(self.writes()), 2)

    def test_durable_replay_new_request_restart_reconnect_and_account_binding(self):
        status, first = self.post(CREATE)
        self.assertEqual(status, 200)
        self.cap = self.artifacts.begin_run("42", "2", 60)
        self.calendar.store = GoogleStore(self.root / "vault", permissions_tests.connection_tests.KEY, clock=lambda: self.now)
        self.assertEqual(self.post(CREATE)[1], first)
        self.assertGreaterEqual(self.post({**CREATE, "fields": {**FIELDS, "summary": "Different"}})[0], 400)
        self.finish()
        self.click(self.select_mode(self.menu(True), 1, "Edit"), "Save")
        self.assertEqual(self.post(CREATE)[1], first)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.post({"operation": "calendar_operation_status", "calendar_id": CALENDAR, "operation_key": CREATE["operation_key"]})[1], first)
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            active = self.store.open("42", row["generation"], "active", row["active"])
            active["sub"] = "other-synthetic-account"
            db.execute("UPDATE connections SET active=? WHERE chat='42'", (self.store.seal("42", row["generation"], "active", active),))
        self.assertGreaterEqual(self.post(CREATE)[0], 400)
        self.assertEqual(len(self.writes()), 1)
        self.assertNotIn(b"Synthetic event", self.store.path.read_bytes())

    def test_stale_etag_preflight_and_provider_412_do_not_retry(self):
        self.assertEqual(self.post(self.change(etag='"stale"'))[1]["status"], "rejected")
        self.assertEqual(self.writes(), [])
        original = self.calendar.request
        def race(method, *args, **kwargs):
            if method == "PATCH":
                self.events["event_1"]["etag"] = '"raced"'
            return original(method, *args, **kwargs)
        self.calendar.request = race
        value = self.change(key="synthetic-race-0001")
        self.assertEqual(self.post(value)[1]["status"], "rejected")
        self.assertEqual(self.post(value)[1]["status"], "rejected")
        self.assertEqual(len(self.writes()), 1)

    def test_cancel_after_effect_and_lost_response_never_resend(self):
        def cancel():
            with self.database.connect() as db:
                self.artifacts.end_run(db, "42", "1", "failed")
        self.on_effect = cancel
        self.assertNotEqual(self.post(CREATE)[0], 200)
        self.cap = self.artifacts.begin_run("42", "2", 60)
        self.on_effect = None
        self.assertEqual(self.post(CREATE)[1]["status"], "confirmed")
        original = self.calendar.request
        def lost(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == "POST":
                raise TimeoutError("synthetic lost response")
            return result
        self.calendar.request = lost
        value = {**CREATE, "operation_key": "synthetic-lost-0001"}
        self.assertEqual(self.post(value)[0], 503)
        self.assertEqual(self.post(value)[1]["status"], "uncertain")
        self.assertEqual(len(self.writes()), 2)

    def test_journal_failure_after_send_and_concurrent_same_key_never_resend(self):
        original = self.calendar.save
        def fail_confirm(chat, key, saved):
            if saved["status"] == "confirmed":
                raise OSError("synthetic journal failure")
            return original(chat, key, saved)
        self.calendar.save = fail_confirm
        self.assertEqual(self.post(CREATE)[0], 503)
        self.calendar.save = original
        self.assertEqual(self.post(CREATE)[1]["status"], "uncertain")
        value = {**CREATE, "operation_key": "synthetic-concurrent-0001"}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.post(value), range(2)))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(len(self.writes()), 2)

    def test_max_unicode_request_and_response_cross_all_boundaries(self):
        identifier = "\U0001f99b" * 1024
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            revision, settings = self.permissions.read(db, "42", row["generation"])
            settings["calendars"][identifier] = {"title": "Synthetic", "role": "owner", "mode": "edit"}
            self.permissions.write(db, "42", row["generation"], revision + 1, settings)
        self.expected_calendar = identifier
        value = {**CREATE, "calendar_id": identifier, "fields": {**FIELDS, "summary": "\U0001f99b" * 256, "description": "\U0001f99b" * 4000, "location": "\U0001f99b" * 512}}
        self.assertGreater(len(json.dumps(value).encode()), 65536)
        self.assertEqual(self.post(value)[0], 200)
        for index in range(5):
            self.events[f"unicode_{index}"] = event(f"unicode_{index}", **value["fields"])
        client = CalendarClient(self.cap, "10.203.17.30")
        client.url = f"http://127.0.0.1:{self.agent.server_port}/google/worker/calendar"
        result = client.execute({"operation": "list_calendar_events", "calendar_id": identifier,
                                 "time_min": "2026-10-28T00:00:00Z", "time_max": "2026-10-29T00:00:00Z", "limit": 5})
        self.assertEqual(len(result["untrusted_calendar_data"]), 5)
        self.assertTrue(any(len(item["description"]) == 4000 for item in result["untrusted_calendar_data"]))

    def test_create_409_requires_exact_ownership_proof_and_never_overwrites_collision(self):
        original = self.calendar.request
        def duplicate(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == "POST":
                raise CalendarHTTP(409)
            return result
        self.calendar.request = duplicate
        self.assertEqual(self.post(CREATE)[1]["status"], "confirmed")
        self.calendar.request = original
        collision = "a" * 32
        self.events[collision] = event(collision)
        before = copy.deepcopy(self.events[collision])
        value = {**CREATE, "operation_key": "synthetic-collision-0001"}
        with patch("src.google_integration.calendar.secrets.token_hex", return_value=collision):
            self.assertEqual(self.post(value)[0], 503)
        self.assertEqual(self.post(value)[1]["status"], "uncertain")
        self.assertEqual(self.events[collision], before)
        self.assertEqual(len(self.writes()), 2)

    def test_cancel_before_send_and_one_sided_time_validation(self):
        self.artifacts.cancel_run("42", "1")
        self.assertEqual(self.post(CREATE)[0], 403)
        self.assertEqual(self.calls, [])
        self.cap = self.artifacts.begin_run("42", "2", 60)
        value = self.change(fields={"start": {"dateTime": "2026-10-28T12:00:00Z"}})
        self.assertEqual(self.post(value)[1]["status"], "rejected")
        self.assertEqual(self.writes(), [])

    def test_missing_scopes_removed_calendar_role_downgrade_and_other_owner(self):
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            active = self.store.open("42", row["generation"], "active", row["active"])
            active["scopes"] = list(permissions_tests.connection_tests.BASE_SCOPES)
            db.execute("UPDATE connections SET active=? WHERE chat='42'", (self.store.seal("42", row["generation"], "active", active),))
        self.assertGreaterEqual(self.post(CREATE)[0], 400)
        self.assertEqual(self.post({"operation": "list_calendars", "offset": 0, "limit": 20})[1]["untrusted_calendar_data"], [])
        self.assertEqual(self.calls, [])
        self.finish()
        self.click(self.select_mode(self.menu(True), 1, "Edit"), "Save")
        self.transport.pages = [{"items": [{"id": CALENDAR, "summary": "Synthetic", "accessRole": "reader"}]}]
        self.menu(True)
        self.assertGreaterEqual(self.post(CREATE)[0], 400)
        self.transport.pages = [{"items": []}]
        self.menu(True)
        self.assertGreaterEqual(self.post(CREATE)[0], 400)
        self.assertEqual(self.writes(), [])
        self.finish("43")
        with self.store.transaction() as db:
            row = self.store.connection(db, "43")
            self.permissions.write(db, "43", row["generation"], 1, {"gmail": "off", "calendars": {
                CALENDAR: {"title": "Other owner", "role": "owner", "mode": "edit"}}})
        cap = self.artifacts.begin_run("43", "1", 60)
        self.assertEqual(self.post(CREATE, cap)[0], 200)
        self.assertGreaterEqual(self.post({"operation": "calendar_operation_status", "calendar_id": CALENDAR,
                                          "operation_key": CREATE["operation_key"]})[0], 400)

    def test_changed_permissions_or_account_after_preflight_prevents_mutation(self):
        original = self.calendar.request
        for change in ("permission", "account"):
            with self.subTest(change=change):
                def changed(method, *args, **kwargs):
                    result = original(method, *args, **kwargs)
                    if method == "GET":
                        with self.store.transaction() as db:
                            row = self.store.connection(db, "42")
                            if change == "permission":
                                revision, settings = self.permissions.read(db, "42", row["generation"])
                                self.permissions.write(db, "42", row["generation"], revision + 1, settings)
                            else:
                                active = self.store.open("42", row["generation"], "active", row["active"])
                                active["sub"] = "changed-mid-preflight"
                                db.execute("UPDATE connections SET active=? WHERE chat='42'", (self.store.seal("42", row["generation"], "active", active),))
                    return result
                self.calendar.request = changed
                self.assertGreaterEqual(self.post(self.change(key="synthetic-mid-" + change))[0], 400)
                self.assertEqual(self.writes(), [])

    def test_malformed_success_stays_uncertain_and_failed_claim_sends_nothing(self):
        original = self.calendar.request
        def malformed(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == "POST":
                result["id"] = "unrelated_event"
            return result
        self.calendar.request = malformed
        self.assertEqual(self.post(CREATE)[0], 503)
        self.assertEqual(self.post(CREATE)[1]["status"], "uncertain")
        self.assertEqual(len(self.writes()), 1)
        self.calendar.request = original
        with patch.object(self.calendar, "attempt", side_effect=OSError("synthetic claim failure")):
            self.assertEqual(self.post({**CREATE, "operation_key": "synthetic-claim-failure"})[0], 503)
        self.assertEqual(len(self.writes()), 1)


if __name__ == "__main__":
    unittest.main()
