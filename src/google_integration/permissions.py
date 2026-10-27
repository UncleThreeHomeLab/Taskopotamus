"""Owner-only permission drafts, exact calendar identities, and service dispatch guards."""
import re
import secrets
import time

from cryptography.exceptions import InvalidTag

from .oauth import BASE_SCOPES, GoogleUnavailable, bounded_lock, remaining
from .store import validate_chat


GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_READ = "https://www.googleapis.com/auth/calendar.events.readonly"
CALENDAR_EDIT = "https://www.googleapis.com/auth/calendar.events"
MODES = ("off", "read", "edit")


def plain(value, limit):
    if not isinstance(value, str):
        return "Unnamed calendar"
    return "".join(c if c.isprintable() and c not in "\r\n" else " " for c in value)[:limit]


def valid_calendar_id(value):
    return (isinstance(value, str) and 1 <= len(value) <= 1024 and value != "primary"
            and all(c.isprintable() and not c.isspace() for c in value))


def required_scopes(settings):
    required = set()
    if settings["gmail"] == "read":
        required.add(GMAIL_READ)
    modes = {entry["mode"] for entry in settings["calendars"].values()}
    if "edit" in modes:
        required.add(CALENDAR_EDIT)
    elif "read" in modes:
        required.add(CALENDAR_READ)
    return required


def scopes_allow(required, granted):
    granted = set(granted)
    if CALENDAR_EDIT in granted:
        granted.add(CALENDAR_READ)
    # Google's token endpoint may canonicalize the OIDC email scope.
    if "https://www.googleapis.com/auth/userinfo.email" in granted:
        granted.add("email")
    return required <= granted


class GooglePermissions:
    def __init__(self, oauth):
        self.oauth, self.store = oauth, oauth.store

    def read(self, db, chat, generation):
        row = db.execute("SELECT * FROM permissions WHERE chat=?", (chat,)).fetchone()
        empty = {"gmail": "off", "calendars": {}}
        if row is None or row["generation"] != generation:
            return 0, empty
        try:
            value = self.store.open(chat, generation, f"permissions:{row['revision']}", row["payload"])
            self.validate(value)
            if type(row["revision"]) is not int or row["revision"] < 0:
                raise ValueError()
            return row["revision"], value
        except (InvalidTag, ValueError, TypeError, KeyError, UnicodeError):
            # Corrupt records must also invalidate drafts made from their former permissions.
            return row["revision"] + 1 if type(row["revision"]) is int and row["revision"] >= 0 else 0, empty

    @staticmethod
    def validate(value):
        if (not isinstance(value, dict) or set(value) != {"gmail", "calendars"}
                or value["gmail"] not in ("off", "read") or not isinstance(value["calendars"], dict)
                or len(value["calendars"]) > 500):
            raise ValueError("Invalid Google permissions.")
        for identifier, entry in value["calendars"].items():
            if (not valid_calendar_id(identifier) or not isinstance(entry, dict)
                    or set(entry) != {"title", "role", "mode"}
                    or not isinstance(entry["title"], str) or len(entry["title"]) > 120
                    or entry["title"] != plain(entry["title"], 120)
                    or entry["role"] not in ("reader", "writer", "owner", "unsupported")
                    or entry["mode"] not in MODES
                    or entry["mode"] == "edit" and entry["role"] not in ("writer", "owner")
                    or entry["mode"] != "off" and entry["role"] == "unsupported"):
                raise ValueError("Invalid calendar permission.")

    def write(self, db, chat, generation, revision, settings):
        self.validate(settings)
        db.execute("INSERT INTO permissions VALUES (?,?,?,?) ON CONFLICT(chat) DO UPDATE SET "
                   "generation=excluded.generation,revision=excluded.revision,payload=excluded.payload",
                   (chat, generation, revision, self.store.seal(chat, generation, f"permissions:{revision}", settings)))

    def discover(self, chat, deadline):
        generation, active = self.oauth.credentials(chat, deadline)
        if not scopes_allow({BASE_SCOPES[2]}, active["scopes"]):
            raise PermissionError("Calendar list permission missing. Reconnect Google.")
        catalog, seen, page = {}, set(), None
        for _ in range(5):
            remaining(deadline)
            response = self.oauth.transport.calendar_list(active["access_token"], page, deadline)
            remaining(deadline)
            items = response.get("items", [])
            if not isinstance(items, list) or len(items) > 100:
                raise GoogleUnavailable("Calendar list incomplete. Existing settings preserved.")
            for entry in items:
                if not isinstance(entry, dict) or not valid_calendar_id(entry.get("id")) or entry["id"] in catalog:
                    raise GoogleUnavailable("Calendar list invalid. Existing settings preserved.")
                role = entry.get("accessRole")
                catalog[entry["id"]] = {"title": plain(entry.get("summary"), 120),
                                        "role": role if role in ("reader", "writer", "owner") else "unsupported",
                                        "mode": "off"}
            page = response.get("nextPageToken")
            if page is None:
                return generation, catalog
            if not isinstance(page, str) or not 1 <= len(page) <= 2048 or page in seen:
                break
            seen.add(page)
        raise GoogleUnavailable("Calendar list incomplete. Existing settings preserved.")

    def control(self, chat, request_id, action, token=None):
        validate_chat(chat)
        if action == "callback" and (not isinstance(token, str) or not re.fullmatch(r"g:[A-Za-z0-9_-]{32}", token)):
            raise ValueError("Invalid Google button.")
        identity = action + (":" + token if action == "callback" else "")
        deadline = time.monotonic() + 20
        with bounded_lock(self.oauth.lifecycle_lock, deadline):
            with self.store.transaction(deadline) as db:
                cached = self.store.cached(db, chat, request_id, identity)
                if cached is not None:
                    return cached
                if action == "menu_calendars":
                    db.execute("DELETE FROM menus WHERE chat=?", (chat,))
                current = self.store.connection(db, chat)
                login_pending = db.execute("SELECT 1 FROM flows WHERE chat=?", (chat,)).fetchone() is not None
                can_discover = current["status"] == "active" and bool(current["active"]) and not current["pending"] and not login_pending
                missing_list_scope = False
                if can_discover:
                    active = self.store.open(chat, current["generation"], "active", current["active"])
                    missing_list_scope = not scopes_allow({BASE_SCOPES[2]}, active["scopes"])
            catalog = None
            if action == "menu_calendars" and can_discover and not missing_list_scope:
                try:
                    discovered_generation, catalog = self.discover(chat, deadline)
                except (GoogleUnavailable, PermissionError):
                    # A failed refresh never leaves an older Save button usable.
                    with self.store.transaction(deadline) as db:
                        db.execute("DELETE FROM menus WHERE chat=?", (chat,))
                        result = {"state": "menu", "text": "Calendar list unavailable or incomplete. Existing settings preserved. Send /calendars to retry, or /google connect to renew consent. All access resets Off when reconnecting.", "keyboard": []}
                        self.store.save_command(db, chat, request_id, identity, result)
                    return result
            transition = None
            remaining(deadline)
            with self.store.transaction(deadline) as db:
                row = self.store.connection(db, chat)
                generation = row["generation"]
                revision, settings = self.read(db, chat, generation)
                if action == "callback":
                    menu_row = db.execute("SELECT * FROM menus WHERE chat=?", (chat,)).fetchone()
                    if (menu_row is None or menu_row["generation"] != generation or menu_row["revision"] != revision
                            or menu_row["expires"] <= self.store.clock()):
                        raise ValueError("Google menu expired. Send /google or /calendars.")
                    menu = self.store.open(chat, generation, f"menu:{revision}", menu_row["payload"])
                    if menu.get("expires") != menu_row["expires"] or menu["expires"] <= self.store.clock():
                        raise ValueError("Google menu expired.")
                    selected = menu["actions"].get(token)
                    if selected is None:
                        raise ValueError("Google button expired.")
                    kind, argument = selected
                    if kind in ("connect", "disconnect", "confirm"):
                        transition = (kind, argument, None)
                    elif kind == "reauthorize":
                        transition = ("connect", None, list(BASE_SCOPES))
                    elif kind == "disconnect_review":
                        menu["view"] = "disconnect"
                    elif kind == "gmail":
                        menu["draft"]["gmail"] = argument
                    elif kind == "calendar":
                        identifier, mode = argument
                        menu["draft"]["calendars"][identifier]["mode"] = mode
                        menu["view"], menu["calendar_id"] = "calendar_detail", identifier
                    elif kind == "calendar_detail":
                        menu["view"], menu["calendar_id"] = "calendar_detail", argument
                    elif kind == "calendar_list":
                        menu["view"] = "calendars"
                        menu.pop("calendar_id", None)
                    elif kind == "page":
                        menu["page"] = argument
                    elif kind == "save":
                        if (row["status"] != "active" or not row["active"] or row["pending"]
                                or db.execute("SELECT 1 FROM flows WHERE chat=?", (chat,)).fetchone()):
                            raise ValueError("Google connection changed. Send /google.")
                        draft = menu["draft"]
                        self.validate(draft)
                        active = self.store.open(chat, generation, "active", row["active"])
                        needed = required_scopes(draft)
                        increasing = (draft["gmail"] == "read" and settings["gmail"] == "off"
                                      or any(MODES.index(entry["mode"]) > MODES.index(settings["calendars"].get(identifier, {"mode": "off"})["mode"])
                                             for identifier, entry in draft["calendars"].items()))
                        granted = scopes_allow(needed, active["scopes"])
                        if increasing and not granted:
                            transition = ("connect", None, sorted(set(BASE_SCOPES) | needed))
                        else:
                            # The read, revision comparison and write share one immediate transaction.
                            revision += 1
                            self.write(db, chat, generation, revision, draft)
                            settings = draft
                            menu["draft"] = settings
                            menu["notice"] = "Saved." if granted else "Saved. Missing Google consent keeps affected operations unavailable. Reconnect with /google connect if needed."
                    else:
                        raise ValueError("Unknown Google button.")
                else:
                    if catalog is not None:
                        if discovered_generation != generation:
                            raise ValueError("Google connection changed.")
                        for identifier, entry in catalog.items():
                            old = settings["calendars"].get(identifier, {}).get("mode", "off")
                            if entry["role"] in ("writer", "owner") or entry["role"] == "reader" and old == "read":
                                entry["mode"] = old
                        settings["calendars"] = catalog
                        revision += 1
                        self.write(db, chat, generation, revision, settings)
                    menu = {"view": "calendars" if action == "menu_calendars" else "google",
                            "draft": settings, "page": 0}
                    if action == "menu_calendars" and missing_list_scope:
                        menu["view"] = "reauthorize"
                    if login_pending and not row["pending"]:
                        menu["view"] = "login_pending"
                if transition is None:
                    result = self.render(db, chat, row, revision, menu)
                    self.store.save_command(db, chat, request_id, identity, result)
                    return result
            kind, argument, scopes = transition
            return self.oauth._control(chat, request_id, kind, argument, requested_scopes=scopes, identity_override=identity)

    def render(self, db, chat, connection, revision, menu):
        actions, keyboard = {}, []

        def buttons(options):
            row = []
            for label, kind, argument in options:
                token = "g:" + secrets.token_urlsafe(24)
                actions[token] = [kind, argument]
                row.append({"text": label, "callback_data": token})
            keyboard.append(row)

        generation = connection["generation"]
        text = []
        if connection["pending"]:
            pending = self.store.open(chat, generation, "pending", connection["pending"])
            text = ["Google account waiting for confirmation: " + plain(pending["email"], 254),
                    "Confirm only your intended account. All access stays Off; reopen settings and Save after confirmation."]
            buttons([("Confirm account", "confirm", pending["confirmation"])])
        elif menu["view"] == "login_pending":
            text = ["Google login is pending. Finish browser login, then send /google to confirm the account.",
                    "All access is Off. Use Connect Google for a fresh login link."]
            buttons([("Connect Google", "connect", None)])
        elif connection["status"] != "active" or not connection["active"]:
            text = ["Google is not connected. All access is Off."]
            buttons([("Connect Google", "connect", None)])
        elif menu["view"] == "disconnect":
            text = ["Disconnect Google? All local permissions and credentials will be removed."]
            buttons([("Confirm disconnect", "disconnect", None)])
        elif menu["view"] == "reauthorize":
            text = ["Google calendar list consent is missing. Reconnect to request consent again.",
                    "Reconnecting resets ALL access Off. Confirm the account here, reopen settings and Save again."]
            buttons([("Reconnect Google", "reauthorize", None)])
        else:
            active = self.store.open(chat, generation, "active", connection["active"])
            text = ["Google connected: " + plain(active["email"], 254), menu.pop("notice", "Changes are drafts until Save.")]
            if menu["view"] == "google":
                text.append("Gmail: " + menu["draft"]["gmail"].title())
                text.append("Gmail Read sends selected email to the configured AI provider. Email may appear in Telegram and conversation history.")
                buttons([("Gmail Off", "gmail", "off"), ("Gmail Read", "gmail", "read")])
                text.append("Use /calendars for individual calendar access.")
                buttons([("Disconnect Google", "disconnect_review", None)])
            elif menu["view"] == "calendar_detail":
                identifier = menu["calendar_id"]
                entry = menu["draft"]["calendars"][identifier]
                text.extend(["Review calendar before Save:", "Title: " + entry["title"],
                             "Exact calendar ID:", identifier,
                             f"Role: {entry['role']}. Draft access: {entry['mode'].title()}."])
                text.append("Read sends event data to the AI provider; replies/history may retain it. Edit can create, change and delete events, affecting existing participants.")
                modes = MODES if entry["role"] in ("writer", "owner") else MODES[:2] if entry["role"] == "reader" else MODES[:1]
                buttons([(mode.title(), "calendar", [identifier, mode]) for mode in modes])
                buttons([("Back to calendars", "calendar_list", None)])
            else:
                entries = list(menu["draft"]["calendars"].items())
                page = menu["page"]
                text.append(f"Calendars: page {page + 1} of {max(1, (len(entries) + 4) // 5)}")
                if not entries:
                    text.append("No calendars available.")
                for index, (identifier, entry) in enumerate(entries[page * 5:page * 5 + 5], page * 5 + 1):
                    text.append(f"{index}. {entry['title']} [{entry['role']}]: {entry['mode'].title()}")
                    buttons([(f"{index} Details", "calendar_detail", identifier)])
                navigation = []
                if page:
                    navigation.append(("Previous", "page", page - 1))
                if (page + 1) * 5 < len(entries):
                    navigation.append(("Next", "page", page + 1))
                if navigation:
                    buttons(navigation)
            text.append("Save may request extra Google consent and reset ALL access Off. After login, confirm here, reopen settings and Save again.")
            buttons([("Save", "save", None)])
        menu["actions"] = actions
        menu.setdefault("expires", self.store.clock() + 600)
        db.execute("INSERT INTO menus VALUES (?,?,?,?,?) ON CONFLICT(chat) DO UPDATE SET generation=excluded.generation,"
                   "revision=excluded.revision,expires=excluded.expires,payload=excluded.payload",
                   (chat, generation, revision, menu["expires"], self.store.seal(chat, generation, f"menu:{revision}", menu)))
        return {"state": "menu", "text": "\n".join(text), "keyboard": keyboard}

    def dispatch(self, chat, operation, callback, calendar_id=None, *, run_guard=None, deadline=None, include_context=False):
        """Internal S3/S4 boundary. Check before refresh and immediately before dispatch, under lifecycle lock."""
        validate_chat(chat)
        if operation not in ("gmail_read", "calendar_read", "calendar_edit", "calendar_list") or not callable(callback):
            raise PermissionError("Google operation denied.")
        deadline = min(deadline, time.monotonic() + 20) if deadline is not None else time.monotonic() + 20
        with bounded_lock(self.oauth.lifecycle_lock, deadline):
            def check():
                nonlocal deadline
                if run_guard is not None:
                    deadline = min(deadline, run_guard())
                with self.store.transaction(deadline) as db:
                    row = self.store.connection(db, chat)
                    revision, settings = self.read(db, chat, row["generation"])
                    if (row["status"] != "active" or not row["active"] or row["pending"]
                            or db.execute("SELECT 1 FROM flows WHERE chat=?", (chat,)).fetchone()):
                        raise PermissionError("Google access is Off.")
                    active = self.store.open(chat, row["generation"], "active", row["active"])
                    if operation == "calendar_list":
                        permitted, scope = calendar_id is None, None
                    elif operation == "gmail_read":
                        permitted = calendar_id is None and settings["gmail"] == "read"
                        scope = GMAIL_READ
                    else:
                        entry = settings["calendars"].get(calendar_id) if valid_calendar_id(calendar_id) else None
                        permitted = entry is not None and (entry["mode"] == "edit" or operation == "calendar_read" and entry["mode"] == "read")
                        scope = CALENDAR_EDIT if operation == "calendar_edit" else CALENDAR_READ
                    if not permitted or scope is not None and not scopes_allow({scope}, active["scopes"]):
                        raise PermissionError("Google access is Off or scope unavailable.")
                    return row["generation"], revision, active["sub"], tuple(sorted(active["scopes"]))
            expected = check()
            generation, active = self.oauth.credentials(chat, deadline, run_guard=run_guard)
            if check() != expected or generation != expected[0] or active["sub"] != expected[2]:
                raise PermissionError("Google permissions changed.")
            remaining(deadline)
            if include_context:
                def dispatch_guard():
                    if check() != expected:
                        raise PermissionError("Google permissions or account changed.")
                    remaining(deadline)
                    return deadline
                with self.store.transaction(deadline) as db:
                    _, settings = self.read(db, chat, generation)
                context = {"sub": active["sub"], "scopes": active["scopes"], "calendars": settings["calendars"], "guard": dispatch_guard}
                return callback(active["access_token"], calendar_id, deadline, context)
            return callback(active["access_token"], calendar_id, deadline)
