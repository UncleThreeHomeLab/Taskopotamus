"""Offline permission, callback, discovery and application-only delivery checks."""
import copy
import http.client
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from database_support import isolated_database
import test_google_connection as connection_tests
from cryptography.exceptions import InvalidTag
from src.google_integration.oauth import BASE_SCOPES, CALENDAR_LIST_URL, GoogleTransport, GoogleUnavailable, TOKEN_URL
from src.google_integration.permissions import CALENDAR_EDIT, CALENDAR_READ, GMAIL_READ
from src.messaging.delivery_store import DeliveryStore
from src.messaging.messages import validate_items
from src.telegram.bot import deliver_one, process_input, reply_for
from src.telegram.inbox import Inbox, normalize


class PermissionGoogle(connection_tests.SyntheticGoogle):
    def __init__(self):
        super().__init__()
        self.scopes = list(BASE_SCOPES)
        self.pages = [{"items": [{"id": "calendar-exact@example.test", "summary": "Synthetic", "accessRole": "owner"}]}]
        self.list_calls, self.timeouts = [], []

    def request(self, url, fields=None, token=None, timeout=10):
        self.timeouts.append(timeout)
        result = super().request(url, fields, token, timeout)
        if url == TOKEN_URL:
            result["scope"] = " ".join(self.scopes)
        return result

    def calendar_list(self, token, page_token, deadline):
        self.list_calls.append((token, page_token, deadline))
        response = self.pages[len(self.list_calls) - 1]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class GooglePermissionTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the synthetic fixture; lifecycle tests remain in their original suite.
        connection_tests.GoogleConnectionTests.setUp(self)
        self.transport = PermissionGoogle()
        self.oauth.transport = self.transport
        self.permissions = self.oauth.permissions

    control = connection_tests.GoogleConnectionTests.control
    start = connection_tests.GoogleConnectionTests.start
    finish = connection_tests.GoogleConnectionTests.finish

    def menu(self, calendars=False):
        self.transport.list_calls.clear()
        return self.control("menu_calendars" if calendars else "menu_google")

    def click(self, menu, label, chat="42", request=None):
        token = next(button["callback_data"] for row in menu["keyboard"] for button in row if button["text"] == label)
        if request is None:
            self.number += 1
            request = str(self.number)
        return self.oauth.control(chat, request, "callback", token)

    def settings(self):
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            return self.permissions.read(db, "42", row["generation"])

    def select_mode(self, menu, number, mode):
        return self.click(self.click(menu, f"{number} Details"), mode)

    def all_scopes(self):
        self.transport.scopes += [GMAIL_READ, CALENDAR_READ, CALENDAR_EDIT]

    def enable(self, gmail=True, mode="edit"):
        self.all_scopes()
        self.finish()
        if gmail:
            menu = self.click(self.menu(), "Gmail Read")
            self.click(menu, "Save")
        menu = self.select_mode(self.menu(True), 1, mode.title())
        self.click(menu, "Save")

    def test_defaults_drafts_save_revision_idempotency_and_refresh_isolation(self):
        self.all_scopes()
        self.finish()
        self.assertEqual(self.settings()[1], {"gmail": "off", "calendars": {}})
        original = self.menu()
        draft = self.click(original, "Gmail Read")
        self.assertEqual(self.settings()[1]["gmail"], "off")
        self.number += 1
        request = str(self.number)
        saved = self.click(draft, "Save", request=request)
        self.assertEqual(self.settings()[1]["gmail"], "read")
        self.assertEqual(self.click(draft, "Save", request=request), saved)
        with self.assertRaises(ValueError):
            self.click(draft, "Save")
        before = self.settings()
        self.now += 3600
        self.oauth.credentials("42")
        self.assertEqual(self.settings(), before)
        self.assertGreater(self.transport.timeouts[-1], 0)
        self.assertLessEqual(self.transport.timeouts[-1], 10)
        with self.assertRaises(ValueError):
            self.click(original, "Gmail Read")

    def test_incremental_consent_resets_all_off_partial_denial_never_restores(self):
        self.finish()
        menu = self.click(self.menu(), "Gmail Read")
        self.assertEqual(len(self.transport.calls), 2)
        login = self.click(menu, "Save")
        self.assertTrue(login["reauthorization"])
        self.assertEqual(self.settings()[1]["gmail"], "off")
        ticket = parse_qs(urlsplit(login["url"]).query)["ticket"][0]
        url, cookie = self.oauth.start(ticket)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(set(query["scope"][0].split()), set(BASE_SCOPES) | {GMAIL_READ})
        self.oauth.callback(query["state"][0], cookie, "synthetic-code")
        pending = self.store.status("42")["pending"]
        self.control("confirm", confirmation=pending["confirmation"])
        self.assertEqual(self.settings()[1]["gmail"], "off")
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "gmail_read", Mock())
        with self.assertRaises(ValueError):
            self.click(menu, "Save")
        retry = self.click(self.menu(), "Gmail Read")
        self.assertEqual(self.click(retry, "Save")["state"], "login")

    def test_calendar_catalog_new_removed_roles_exact_ids_and_failed_snapshots(self):
        self.enable()
        self.transport.pages = [{"items": [{"id": "new-calendar", "summary": "<b>Untrusted</b>\n/command", "accessRole": "reader"},
                                           {"id": "unsupported", "summary": "Busy only", "accessRole": "freeBusyReader"}]}]
        menu = self.menu(True)
        settings = self.settings()[1]
        self.assertNotIn("calendar-exact@example.test", settings["calendars"])
        self.assertEqual(settings["calendars"]["new-calendar"]["mode"], "off")
        self.assertIn("<b>Untrusted</b> /command", menu["text"])
        detail = self.click(menu, "1 Details")
        labels = [button["text"] for row in detail["keyboard"] for button in row]
        self.assertIn("Read", labels)
        self.assertNotIn("Edit", labels)
        detail = self.click(self.click(detail, "Back to calendars"), "2 Details")
        labels = [button["text"] for row in detail["keyboard"] for button in row]
        self.assertNotIn("Read", labels)
        self.assertNotIn("Edit", labels)
        menu = detail
        before = self.settings()
        self.transport.pages = [{"items": [], "nextPageToken": "repeated"}, {"items": [], "nextPageToken": "repeated"}]
        failed = self.menu(True)
        self.assertEqual(failed["keyboard"], [])
        self.assertEqual(self.settings(), before)
        with self.assertRaises(ValueError):
            self.click(menu, "Save")
        self.transport.pages = [{"items": [], "nextPageToken": str(index)} for index in range(5)]
        self.assertEqual(self.menu(True)["keyboard"], [])
        self.assertEqual(len(self.transport.list_calls), 5)
        self.assertEqual(self.settings(), before)

    def test_pagination_and_role_downgrade_never_keep_edit(self):
        self.enable()
        entries = [{"id": str(index), "summary": "Same title", "accessRole": "owner"} for index in range(7)]
        self.transport.pages = [{"items": entries[:4], "nextPageToken": "next"}, {"items": entries[4:]}]
        menu = self.menu(True)
        self.assertIn("page 1 of 2", menu["text"])
        next_page = self.click(menu, "Next")
        self.assertIn("6. Same title", next_page["text"])
        self.assertNotIn("1. Same title", next_page["text"])
        self.assertEqual(len(self.transport.list_calls), 2)
        detail = self.click(next_page, "6 Details")
        self.assertIn("Exact calendar ID:\n5\n", detail["text"])
        listing = self.click(self.click(detail, "Read"), "Back to calendars")
        self.assertIn("page 2 of 2", listing["text"])
        self.assertIn("6. Same title [owner]: Read", listing["text"])
        self.transport.pages = [{"items": [{"id": "calendar-exact@example.test", "accessRole": "reader"}]}]
        self.menu(True)
        self.assertEqual(self.settings()[1]["calendars"]["calendar-exact@example.test"]["mode"], "off")

    def test_ambiguous_titles_require_exact_identity_review_before_save_and_back_preserves_draft(self):
        self.all_scopes()
        self.transport.email = "x" * 241 + "@example.test"
        self.finish()
        longest_id = "a" * 1011 + "@example.test"
        self.assertEqual(len(longest_id), 1024)
        entries = [{"id": "exact-first", "summary": "Same title", "accessRole": "owner"},
                   {"id": "exact-second", "summary": "Same title", "accessRole": "owner"},
                   {"id": longest_id, "summary": "x" * 120 + "first", "accessRole": "owner"},
                   {"id": "truncated-sibling", "summary": "x" * 120 + "second", "accessRole": "owner"}]
        self.transport.pages = [{"items": entries}]
        first = self.click(self.menu(True), "1 Details")
        self.assertIn("Exact calendar ID:\nexact-first\n", first["text"])
        self.assertIn("Draft access: Off", first["text"])
        self.assertTrue(all(entry["mode"] == "off" for entry in self.settings()[1]["calendars"].values()))
        listing = self.click(first, "Back to calendars")
        self.assertIn("1. Same title [owner]: Off", listing["text"])
        with self.assertRaises(ValueError):
            self.click(first, "Save")
        second = self.click(listing, "2 Details")
        self.assertIn("Exact calendar ID:\nexact-second\n", second["text"])
        self.assertNotIn("exact-first", second["text"])
        listing = self.click(self.click(second, "Edit"), "Back to calendars")
        self.assertIn("2. Same title [owner]: Edit", listing["text"])
        third = self.click(listing, "3 Details")
        self.assertIn("Exact calendar ID:\n" + longest_id + "\n", third["text"])
        self.assertNotIn("truncated-sibling", third["text"])
        self.assertTrue(all(entry["mode"] == "off" for entry in self.settings()[1]["calendars"].values()))
        DeliveryStore.validate_google_menu(third["text"], third["keyboard"])
        self.assertLessEqual(len(third["text"]), 2000)
        listing = self.click(third, "Back to calendars")
        fourth = self.click(listing, "4 Details")
        self.assertIn("Exact calendar ID:\ntruncated-sibling\n", fourth["text"])
        saved = self.click(self.click(fourth, "Read"), "Save")
        DeliveryStore.validate_google_menu(saved["text"], saved["keyboard"])
        modes = {identifier: entry["mode"] for identifier, entry in self.settings()[1]["calendars"].items()}
        self.assertEqual(modes, {"exact-first": "off", "exact-second": "edit", longest_id: "off", "truncated-sibling": "read"})

    def test_guard_mode_scope_and_exact_id_matrix_denies_before_refresh_or_data(self):
        self.enable()
        dispatch = Mock(return_value="synthetic-result")
        for operation in ("gmail_read", "calendar_read", "calendar_edit"):
            identifier = None if operation == "gmail_read" else "calendar-exact@example.test"
            self.assertEqual(self.permissions.dispatch("42", operation, dispatch, identifier), "synthetic-result")
        menu = self.select_mode(self.menu(True), 1, "Read")
        self.click(menu, "Save")
        calls = len(self.transport.calls)
        dispatch.reset_mock()
        for operation, identifier in (("calendar_edit", "calendar-exact@example.test"), ("calendar_read", "primary"),
                                      ("calendar_read", "unknown"), ("calendar_read", None), ("gmail_read", "arbitrary")):
            with self.subTest(operation=operation, identifier=identifier), self.assertRaises(PermissionError):
                self.permissions.dispatch("42", operation, dispatch, identifier)
        self.assertEqual(len(self.transport.calls), calls)
        dispatch.assert_not_called()
        menu = self.select_mode(self.menu(True), 1, "Off")
        self.click(menu, "Save")
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "calendar_read", dispatch, "calendar-exact@example.test")
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            active = self.store.open("42", row["generation"], "active", row["active"])
            active["scopes"] = list(BASE_SCOPES)
            db.execute("UPDATE connections SET active=? WHERE chat='42'",
                       (self.store.seal("42", row["generation"], "active", active),))
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "gmail_read", dispatch)
        dispatch.assert_not_called()

    def test_refresh_rechecks_revision_generation_and_permissions_are_encrypted(self):
        self.enable()
        self.assertNotIn(b"calendar-exact@example.test", self.store.path.read_bytes())
        self.assertNotIn(b"Synthetic", self.store.path.read_bytes())
        self.now += 3600
        self.transport.on_token = lambda: self.control("connect")
        callback = Mock()
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "gmail_read", callback)
        callback.assert_not_called()
        self.assertEqual(self.settings()[1]["gmail"], "off")

    def test_malformed_permissions_and_expired_tampered_other_owner_buttons_deny(self):
        self.enable()
        menu = self.menu()
        for label, chat in (("Gmail Off", "43"),):
            with self.assertRaises(ValueError):
                self.click(menu, label, chat)
        with self.assertRaises(ValueError):
            self.oauth.control("42", "900", "callback", "g:" + "x" * 32)
        with self.store.transaction() as db:
            db.execute("UPDATE permissions SET payload=? WHERE chat='42'", (b"invalid",))
        callback = Mock()
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "gmail_read", callback)
        callback.assert_not_called()
        with self.assertRaises(ValueError):
            self.click(menu, "Save")
        menu = self.menu()
        self.now += 601
        with self.assertRaises(ValueError):
            self.click(menu, "Save")

    def test_revision_authenticated_ciphertext_and_stale_menu_cannot_enable(self):
        self.enable()
        menu = self.menu()
        with self.store.transaction() as db:
            db.execute("UPDATE permissions SET revision=revision+1 WHERE chat='42'")
        self.assertEqual(self.settings()[1]["gmail"], "off")
        with self.assertRaises(ValueError):
            self.click(menu, "Save")
        menu = self.menu()
        with self.store.transaction() as db:
            db.execute("UPDATE menus SET expires=expires+600 WHERE chat='42'")
        with self.assertRaises(ValueError):
            self.click(menu, "Gmail Read")
        menu = self.menu()
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM menus WHERE chat='42'").fetchone()
            db.execute("UPDATE menus SET payload=? WHERE chat='42'", (row["payload"][:-1] + bytes([row["payload"][-1] ^ 1]),))
        with self.assertRaises(InvalidTag):
            self.click(menu, "Gmail Read")
        self.assertEqual(self.settings()[1]["gmail"], "off")

    def test_connection_buttons_and_partial_calendar_list_consent_have_recovery(self):
        disconnected = self.menu(True)
        self.assertEqual(self.transport.list_calls, [])
        login = self.click(disconnected, "Connect Google")
        ticket = parse_qs(urlsplit(login["url"]).query)["ticket"][0]
        url, cookie = self.oauth.start(ticket)
        self.transport.scopes = ["openid", "email", GMAIL_READ]
        self.oauth.callback(parse_qs(urlsplit(url).query)["state"][0], cookie, "synthetic-code")
        pending = self.menu(True)
        self.assertEqual(self.transport.list_calls, [])
        self.click(pending, "Confirm account")
        missing = self.menu(True)
        self.assertIn("consent is missing", missing["text"])
        self.assertEqual(self.transport.list_calls, [])
        menu = self.click(self.menu(), "Gmail Read")
        self.assertEqual(self.click(menu, "Save")["state"], "menu")
        self.assertEqual(self.settings()[1]["gmail"], "read")
        self.assertEqual(self.permissions.dispatch("42", "gmail_read", lambda *_: "allowed"), "allowed")
        off = self.click(self.menu(), "Gmail Off")
        self.assertEqual(self.click(off, "Save")["state"], "menu")
        self.assertEqual(self.settings()[1]["gmail"], "off")
        missing = self.menu(True)
        self.assertTrue(self.click(missing, "Reconnect Google")["reauthorization"])
        waiting = self.menu(True)
        self.assertIn("login is pending", waiting["text"])
        self.assertNotIn("Save", [button["text"] for row in waiting["keyboard"] for button in row])
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "gmail_read", Mock())

    def test_permission_reductions_never_require_consent_even_if_other_scope_was_lost(self):
        self.enable()
        with self.store.transaction() as db:
            row = self.store.connection(db, "42")
            active = self.store.open("42", row["generation"], "active", row["active"])
            active["scopes"] = list(BASE_SCOPES)
            db.execute("UPDATE connections SET active=? WHERE chat='42'",
                       (self.store.seal("42", row["generation"], "active", active),))
        off = self.click(self.menu(), "Gmail Off")
        result = self.click(off, "Save")
        self.assertEqual(result["state"], "menu")
        self.assertIn("unavailable", result["text"])
        self.assertEqual(self.settings()[1]["gmail"], "off")

    def test_disconnect_buttons_need_second_confirmation_and_cached_action_never_repeats(self):
        self.enable()
        before = len(self.transport.calls)
        review = self.click(self.menu(), "Disconnect Google")
        self.assertEqual(self.store.status("42")["state"], "active")
        self.assertEqual(len(self.transport.calls), before)
        self.number += 1
        request = str(self.number)
        disconnected = self.click(review, "Confirm disconnect", request=request)
        self.assertEqual(self.store.status("42")["state"], "disconnected")
        self.assertEqual(self.settings()[1], {"gmail": "off", "calendars": {}})
        self.finish()
        before = len(self.transport.calls)
        self.assertEqual(self.click(review, "Confirm disconnect", request=request), disconnected)
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(self.store.status("42")["state"], "active")
        with self.assertRaises(ValueError):
            self.click(review, "Confirm disconnect")

    def test_refresh_revision_race_is_checked_before_service_dispatch(self):
        self.enable()
        self.now += 3600

        def change_settings():
            with self.store.transaction() as db:
                row = self.store.connection(db, "42")
                revision, settings = self.permissions.read(db, "42", row["generation"])
                settings["gmail"] = "off"
                self.permissions.write(db, "42", row["generation"], revision + 1, settings)

        self.transport.on_token = change_settings
        callback = Mock()
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "gmail_read", callback)
        callback.assert_not_called()
        self.assertEqual(self.settings()[1]["gmail"], "off")

    def test_refresh_omitted_scope_preserves_grant_but_explicit_empty_scope_denies(self):
        self.enable()
        original = self.transport.request
        explicit_empty = False

        def refresh_scope(url, fields=None, token=None, timeout=10):
            response = original(url, fields, token, timeout)
            if url == TOKEN_URL and fields.get("grant_type") == "refresh_token":
                response.pop("scope", None)
                if explicit_empty:
                    response["scope"] = ""
            return response

        self.transport.request = refresh_scope
        self.now += 3600
        callback = Mock(return_value="allowed")
        self.assertEqual(self.permissions.dispatch("42", "gmail_read", callback), "allowed")
        explicit_empty = True
        self.now += 3600
        callback.reset_mock()
        with self.assertRaises(PermissionError):
            self.permissions.dispatch("42", "gmail_read", callback)
        callback.assert_not_called()
        self.assertEqual(self.oauth.credentials("42")[1]["scopes"], [])

    def test_invalid_or_oversized_catalog_never_replaces_settings(self):
        self.enable()
        before = self.settings()
        catalogs = ([{"id": "primary"}], [{"id": "duplicate"}, {"id": "duplicate"}], [{"id": str(i)} for i in range(101)],
                    [{"id": "white space"}], [{"id": "x" * 1025}])
        for catalog in catalogs:
            self.transport.pages = [{"items": catalog}]
            self.assertEqual(self.menu(True)["keyboard"], [])
            self.assertEqual(self.settings(), before)

    def test_calendar_transport_has_only_fixed_endpoint_bounds_and_no_redirect_proxy_authority(self):
        transport = GoogleTransport()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read1.side_effect = [b'{"items":[]}', b'']
        with patch.object(transport.opener, "open", return_value=response) as send:
            self.assertEqual(transport.calendar_list("synthetic-only", "https://evil.test/path?a=b", time.monotonic() + 2), {"items": []})
        request = send.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).scheme + "://" + urlsplit(request.full_url).netloc + urlsplit(request.full_url).path, CALENDAR_LIST_URL)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["maxResults"], ["100"])
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["pageToken"], ["https://evil.test/path?a=b"])
        self.assertLessEqual(send.call_args.kwargs["timeout"], 2)
        response.fp.raw._sock.settimeout.assert_called()
        with self.assertRaises(ValueError):
            transport.request(CALENDAR_LIST_URL + "/arbitrary")
        incoming, outgoing = socket.socketpair()
        self.addCleanup(incoming.close)
        self.addCleanup(outgoing.close)
        outgoing.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n{"items":[]}')
        response = http.client.HTTPResponse(incoming)
        response.begin()
        with patch.object(transport.opener, "open", return_value=response):
            self.assertEqual(transport.calendar_list("synthetic-only", None, time.monotonic() + 2), {"items": []})

    def test_discovery_deadline_lock_waits_and_stale_catalog_fail_closed(self):
        self.finish()
        original = self.transport.calendar_list

        def expired(*args):
            with patch("src.google_integration.oauth.time.monotonic", return_value=args[2] + 1):
                from src.google_integration.oauth import remaining
                remaining(args[2])
            return original(*args)

        self.transport.calendar_list = expired
        self.assertEqual(self.menu(True)["keyboard"], [])
        self.assertEqual(self.settings()[1]["calendars"], {})
        locked, release = threading.Event(), threading.Event()

        def hold():
            with self.oauth.lifecycle_lock:
                locked.set()
                release.wait(2)

        thread = threading.Thread(target=hold)
        thread.start()
        locked.wait(1)
        try:
            with self.assertRaises(GoogleUnavailable):
                self.oauth.credentials("42", time.monotonic() + 0.02)
        finally:
            release.set()
            thread.join(2)


class GoogleControlDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.database = isolated_database(self)
        self.now = 1000.0
        self.deliveries = DeliveryStore(self.database, clock=lambda: self.now, attempt_timeout=10)

    def test_application_menu_is_plain_reserved_bounded_and_native_items_reject_controls(self):
        keyboard = [[{"text": "Gmail Read", "callback_data": "g:" + "a" * 32}]]
        with self.assertRaises(ValueError):
            validate_items([{"type": "text", "text": "x", "reply_markup": keyboard}])
        other = self.deliveries.reserve_command("42", "1", "/ping")
        with self.assertRaises(ValueError):
            self.deliveries.complete_command("42", "1", other["attempt"], "x", keyboard)
        reservation = self.deliveries.reserve_command("42", "2", "/google")
        self.assertEqual(self.deliveries.reserve_command("42", "2", "/google"), reservation)
        text = "Google: [malicious](https://evil.test) <b>email@example.test</b>"
        reply = self.deliveries.complete_command("42", "2", reservation["attempt"], text, keyboard)
        self.assertEqual(self.deliveries.complete_command("42", "2", reservation["attempt"], text, keyboard), reply)
        claimed = self.deliveries.claim(["42"])
        self.assertEqual(claimed["item"], {"type": "text", "text": text})
        self.assertEqual(claimed["google_keyboard"], keyboard)
        self.now += 11
        self.assertIsNone(self.deliveries.claim(["42"]))
        self.assertEqual(self.deliveries.status("42")[0]["state"], "uncertain")
        self.assertNotIn("google_keyboard", reply)
        for invalid in (None, [[{"text": "x", "url": "https://evil.test"}]], keyboard * 10):
            with self.assertRaises(ValueError):
                self.deliveries.validate_google_menu("x", invalid)

    def test_telegram_adapter_attaches_keyboard_without_formatting_and_preserves_uncertainty(self):
        agent = Mock()
        keyboard = [[{"text": "Save", "callback_data": "g:" + "a" * 32}]]
        agent.claim_delivery.return_value = {"item": {"type": "text", "text": "<untrusted>"}, "chat_id": "42", "google_keyboard": keyboard}
        with patch("src.telegram.bot.api", return_value={"message_id": 12}) as send:
            self.assertTrue(deliver_one(agent, "synthetic-only", {"42"}))
        body = send.call_args.args[2]
        self.assertEqual(body["text"], "<untrusted>")
        self.assertEqual(body["reply_markup"], {"inline_keyboard": keyboard})
        self.assertNotIn("parse_mode", body)
        with patch("src.telegram.bot.api", side_effect=RuntimeError("synthetic-uncertainty")):
            deliver_one(agent, "synthetic-only", {"42"})
        self.assertEqual(agent.acknowledge_delivery.call_args.args[1], "uncertain")

    def test_callbacks_are_persisted_before_cursor_and_provenance_survives_reconstruction(self):
        # Inbox's cursor migration needs the existing Telegram polling table.
        from src.telegram.delivery_store import DeliveryStore as PollingStore
        polling = PollingStore(self.database)
        inbox = Inbox(self.database)
        callback = {"id": "synthetic-query", "from": {"id": 42}, "data": "g:" + "a" * 32,
                    "message": {"message_id": 12, "chat": {"id": 42, "type": "private"}, "text": "Untrusted button title"}}
        update = {"update_id": 1, "callback_query": callback}
        inbox.receive(update, {"42"})
        inbox.receive(update, {"42"})
        self.assertEqual(inbox.offset, 2)
        job = inbox.claim()
        self.assertEqual(job["messages"][0]["callback"]["data"], callback["data"])
        agent, google = Mock(), Mock()
        agent.reserve_command.return_value = {"reply": None, "attempt": "a" * 32}
        google.command.return_value = {"google_control": True, "text": "Draft", "keyboard": []}
        with patch("src.telegram.bot.api") as api:
            process_input(job, inbox, {"42"}, agent, "synthetic-token", polling, threading.Event(), google)
        api.assert_called_once_with("synthetic-token", "answerCallbackQuery", {"callback_query_id": "synthetic-query"}, timeout=3)
        google.command.assert_called_once_with(42, "1", "/google_callback " + callback["data"])
        agent.publish_google_control.assert_called_once()
        agent.chat.assert_not_called()
        self.assertIsNone(inbox.claim())

    def test_owner_forward_quote_callback_shape_and_reserved_command_forgery_rejected(self):
        message = {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/google disconnect"}
        google = Mock()
        for extra in ({"forward_origin": {}}, {"quote": {"text": "x"}}, {"reply_to_message": {}}, {"via_bot": {}}):
            unsafe = {**message, **extra}
            self.assertIsNone(normalize({"message": unsafe}, {"42"}))
            self.assertIsNone(reply_for(unsafe, {"42"}, _google=google))
        self.assertIsNone(reply_for({**message, "text": "/google_callback g:" + "a" * 32}, {"42"}, _google=google))
        callback = {"id": "synthetic", "from": {"id": 42}, "data": "g:" + "a" * 32,
                    "message": {"message_id": 12, "chat": {"id": 42, "type": "private"}}}
        for change in ({"from": {"id": 43}}, {"data": "g:" + "x" * 80}, {"message": {"chat": {"id": 42, "type": "private"}}},
                       {"message": {"message_id": 0, "chat": {"id": 42, "type": "private"}}}):
            self.assertIsNone(normalize({"callback_query": {**callback, **change}}, {"42"}))
        google.command.assert_not_called()
