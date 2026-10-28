"""Bounded Google OAuth transport and service-owned connection lifecycle."""
import json
import secrets
import threading
import time
from contextlib import contextmanager
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .store import digest, validate_chat
from .data_protocol import GoogleUnavailable, NoRedirects, remaining, bounded_body


BASE_SCOPES = ("openid", "email", "https://www.googleapis.com/auth/calendar.calendarlist.readonly")
TOKEN_URL = "https://oauth2.googleapis.com/token"
IDENTITY_URL = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


class GrantExpired(GoogleUnavailable):
    pass


class GoogleTransport:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, url, fields=None, token=None, timeout=10):
        if url not in (TOKEN_URL, IDENTITY_URL, REVOKE_URL):
            raise ValueError("Unsupported Google endpoint.")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(url, data=urlencode(fields).encode() if fields is not None else None, headers=headers)
        deadline = time.monotonic() + timeout
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = bounded_body(response, 65536, deadline)
            if url == REVOKE_URL:
                return {}
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except HTTPError as error:
            expired = False
            try:
                body = bounded_body(error.fp, 65536, deadline)
                expired = url == TOKEN_URL and json.loads(body).get("error") == "invalid_grant"
            except (ValueError, AttributeError, OSError):
                pass
            error.close()
            if expired:
                raise GrantExpired("Google authorization expired. Reconnect in Telegram.") from None
            raise GoogleUnavailable("Google request could not be completed.") from None
        except (URLError, OSError, ValueError, HTTPException):
            raise GoogleUnavailable("Google request could not be completed.") from None

    def calendar_list(self, token, page_token, deadline):
        fields = {"maxResults": 100}
        if page_token:
            fields["pageToken"] = page_token
        request = Request(CALENDAR_LIST_URL + "?" + urlencode(fields),
                          headers={"Authorization": "Bearer " + token})
        try:
            with self.opener.open(request, timeout=remaining(deadline)) as response:
                body = bounded_body(response, 262144, deadline)
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except HTTPError as error:
            error.close()
            raise GoogleUnavailable("Calendar list unavailable.") from None
        except (URLError, OSError, ValueError, HTTPException):
            raise GoogleUnavailable("Calendar list unavailable.") from None


@contextmanager
def bounded_lock(lock, deadline):
    acquired = lock.acquire(timeout=remaining(deadline))
    if not acquired:
        raise GoogleUnavailable("Google request timed out.")
    try:
        yield
    finally:
        lock.release()


class GoogleOAuth:
    def __init__(self, config, store, transport=None):
        self.config, self.store = config, store
        self.transport = transport or GoogleTransport()
        self.refresh_lock = threading.Lock()
        self.lifecycle_lock = threading.RLock()
        from .permissions import GooglePermissions
        self.permissions = GooglePermissions(self)

    def control(self, chat, request_id, action, confirmation=None):
        if action in ("menu_google", "menu_calendars", "callback"):
            return self.permissions.control(chat, request_id, action, confirmation)
        with self.lifecycle_lock:
            return self._control(chat, request_id, action, confirmation)

    def _control(self, chat, request_id, action, confirmation=None, requested_scopes=None, identity_override=None, resume=None):
        validate_chat(chat)
        if action == "status":
            return self.store.status(chat)
        if action not in ("connect", "confirm", "disconnect"):
            raise ValueError("Unknown Google command.")
        if action == "confirm" and (not isinstance(confirmation, str) or len(confirmation) > 100):
            raise ValueError("Invalid Google confirmation.")
        identity = identity_override or action + (":" + confirmation if action == "confirm" else "")
        if action == "confirm":
            return self._confirm(chat, request_id, identity, confirmation)
        revoke_token = None
        with self.store.transaction() as db:
            saved = self.store.cached(db, chat, request_id, identity)
            if saved is not None:
                return saved
            row = self.store.connection(db, chat)
            generation = row["generation"]
            if action == "connect":
                if resume is not None:
                    revision, _ = self.permissions.read(db, chat, generation)
                    active = self.store.open(chat, generation, "active", row["active"]) if row["active"] else {}
                    if (row["status"] != "active" or row["pending"]
                            or db.execute("SELECT 1 FROM flows WHERE chat=?", (chat,)).fetchone()
                            or resume["generation"] != generation or resume["revision"] != revision
                            or resume["sub"] != active.get("sub")):
                        raise ValueError("Google permission request changed. Send /google.")
                    self.permissions.validate(resume["settings"])
                    resume = {**resume, "expires": self.store.clock() + 600}
                generation += 1
                retained = None
                if row["active"]:
                    retained = self.store.open(chat, row["generation"], "active", row["active"])
                    retained["permissions"] = {"gmail": "off", "calendars": {}}
                    retained = self.store.seal(chat, generation, "active", retained)
                ticket = secrets.token_urlsafe(32)
                db.execute("DELETE FROM flows WHERE chat=?", (chat,))
                db.execute("UPDATE connections SET generation=?,active=?,pending=NULL,pending_hash=NULL,pending_until=NULL WHERE chat=?",
                           (generation, retained, chat))
                db.execute("INSERT INTO flows VALUES (?,?,?,?,?,?,?)", (
                    chat, generation, digest(ticket), None, None,
                    self.store.seal(chat, generation, "flow", {"scopes": list(requested_scopes or BASE_SCOPES),
                                                              **({"resume": resume} if resume is not None else {})}), self.store.clock() + 600))
                result = {"state": "login", "url": self.config.origin + "/oauth/google/start?" + urlencode({"ticket": ticket})}
                if requested_scopes:
                    result["reauthorization"] = True
                if resume is not None:
                    result["permissions_retained"] = True
            else:
                if row["active"]:
                    active = self.store.open(chat, generation, "active", row["active"])
                    revoke_token = active.get("refresh_token") or active.get("access_token")
                elif row["pending"]:
                    pending = self.store.open(chat, generation, "pending", row["pending"])
                    revoke_token = pending.get("refresh_token") or pending.get("access_token")
                db.execute("DELETE FROM flows WHERE chat=?", (chat,))
                db.execute("UPDATE connections SET generation=generation+1,status='disconnected',active=NULL,pending=NULL,pending_hash=NULL,pending_until=NULL WHERE chat=?", (chat,))
                result = {"state": "disconnected", "revoked": None}
            db.execute("DELETE FROM permissions WHERE chat=?", (chat,))
            db.execute("DELETE FROM menus WHERE chat=?", (chat,))
            self.store.save_command(db, chat, request_id, identity, result)
        if revoke_token:
            try:
                self.transport.request(REVOKE_URL, {"token": revoke_token})
                result["revoked"] = True
            except GoogleUnavailable:
                result["revoked"] = False
            with self.store.transaction() as db:
                cached = db.execute("SELECT generation FROM commands WHERE chat=? AND request_id=?", (chat, request_id)).fetchone()
                if cached:
                    db.execute("UPDATE commands SET response=? WHERE chat=? AND request_id=?",
                               (self.store.seal(chat, cached["generation"], "command:" + request_id, result), chat, request_id))
        return result

    def _confirm(self, chat, request_id, identity, confirmation):
        deadline = time.monotonic() + 20
        with self.store.transaction(deadline) as db:
            saved = self.store.cached(db, chat, request_id, identity)
            if saved is not None:
                return saved
            row = self.store.connection(db, chat)
            if (not row["pending"] or row["pending_until"] <= self.store.clock()
                    or not secrets.compare_digest(digest(confirmation), row["pending_hash"])):
                raise ValueError("Google confirmation expired or unavailable. Send /google.")
            generation, expected_hash, expected_until = row["generation"], row["pending_hash"], row["pending_until"]
            revision, _ = self.permissions.read(db, chat, generation)
            pending = self.store.open(chat, generation, "pending", row["pending"])
        settings, notice = {"gmail": "off", "calendars": {}}, None
        if "resume" in pending:
            if revision != 0:
                raise ValueError("Google permissions changed. Send /google.")
            try:
                settings, notice = self.permissions.confirmed_settings(pending, generation, deadline)
            except GoogleUnavailable:
                return {"state": "menu", "text": "Calendar verification unavailable. Your saved choices are still pending. Send /google and retry Confirm account; no new Google login is needed.", "keyboard": []}
        remaining(deadline)
        with self.store.transaction(deadline) as db:
            saved = self.store.cached(db, chat, request_id, identity)
            if saved is not None:
                return saved
            row = self.store.connection(db, chat)
            current_revision, _ = self.permissions.read(db, chat, row["generation"])
            if (row["generation"] != generation or row["pending_hash"] != expected_hash
                    or row["pending_until"] != expected_until or expected_until <= self.store.clock()
                    or not row["pending"] or current_revision != revision
                    or db.execute("SELECT 1 FROM flows WHERE chat=?", (chat,)).fetchone()):
                raise ValueError("Google confirmation expired or changed. Send /google.")
            applied = "resume" in pending
            pending.pop("confirmation")
            pending.pop("resume", None)
            pending["permissions"] = {"gmail": "off", "calendars": {}}
            generation += 1
            db.execute("UPDATE connections SET generation=?,status='active',active=?,pending=NULL,pending_hash=NULL,pending_until=NULL WHERE chat=?",
                       (generation, self.store.seal(chat, generation, "active", pending), chat))
            db.execute("DELETE FROM permissions WHERE chat=?", (chat,))
            if applied:
                self.permissions.write(db, chat, generation, 1, settings)
            db.execute("DELETE FROM menus WHERE chat=?", (chat,))
            result = {"state": "active", "email": pending["email"], "gmail": settings["gmail"], "calendars": "off"}
            if applied:
                result.pop("calendars")
                result.update(permissions_applied=True, notice=notice,
                              calendar_count=sum(entry["mode"] != "off" for entry in settings["calendars"].values()))
            self.store.save_command(db, chat, request_id, identity, result)
            return result

    def start(self, ticket):
        import base64
        import hashlib

        state, cookie, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with self.store.transaction() as db:
            self.store.cleanup(db)
            row = db.execute("SELECT * FROM flows WHERE ticket_hash=?", (digest(ticket),)).fetchone()
            if row is None:
                raise ValueError("Google login link expired. Reconnect in Telegram.")
            flow = self.store.open(row["chat"], row["generation"], "flow", row["payload"])
            scopes = flow.get("scopes", list(BASE_SCOPES))
            db.execute("UPDATE flows SET ticket_hash=NULL,state_hash=?,cookie_hash=?,payload=? WHERE chat=?",
                       (digest(state), digest(cookie), self.store.seal(row["chat"], row["generation"], "flow", {**flow, "verifier": verifier, "scopes": scopes}), row["chat"]))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self.config.client_id, "redirect_uri": self.config.redirect_uri,
            "response_type": "code", "scope": " ".join(scopes), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline",
            "prompt": "consent select_account", "include_granted_scopes": "true",
        }), cookie

    def callback(self, state, cookie, code=None, denied=False):
        # Revocation must finish before another flow can issue a grant for this deployment.
        with self.lifecycle_lock:
            return self._callback(state, cookie, code, denied)

    def _callback(self, state, cookie, code=None, denied=False):
        with self.store.transaction() as db:
            self.store.cleanup(db)
            row = db.execute("SELECT * FROM flows WHERE state_hash=?", (digest(state),)).fetchone()
            if row is None or not secrets.compare_digest(row["cookie_hash"], digest(cookie)):
                raise ValueError("Google login expired or invalid. Reconnect in Telegram.")
            connection = self.store.connection(db, row["chat"])
            if connection["generation"] != row["generation"]:
                raise ValueError("Google login expired or invalid. Reconnect in Telegram.")
            flow = self.store.open(row["chat"], row["generation"], "flow", row["payload"])
            # One-time claim commits before external calls: uncertainty never replays a code exchange.
            db.execute("DELETE FROM flows WHERE chat=?", (row["chat"],))
        if denied:
            return
        tokens = self.transport.request(TOKEN_URL, {"client_id": self.config.client_id,
            "client_secret": self.config.client_secret, "code": code, "code_verifier": flow["verifier"],
            "redirect_uri": self.config.redirect_uri, "grant_type": "authorization_code"})
        tokens = self._tokens(tokens)
        identity = self.transport.request(IDENTITY_URL, token=tokens["access_token"])
        if (not isinstance(identity.get("sub"), str) or not 1 <= len(identity["sub"]) <= 255
                or not isinstance(identity.get("email"), str) or not 3 <= len(identity["email"]) <= 254
                or identity.get("email_verified") is not True
                or any(ord(c) < 33 or ord(c) > 126 for c in identity["email"])):
            raise GoogleUnavailable("Google identity could not be verified.")
        with self.store.transaction() as db:
            connection = self.store.connection(db, row["chat"])
            if connection["generation"] != row["generation"] or row["expires"] <= self.store.clock():
                raise ValueError("Google login expired. Reconnect in Telegram.")
            if connection["active"]:
                active = self.store.open(row["chat"], row["generation"], "active", connection["active"])
                if active["sub"] != identity["sub"]:
                    raise ValueError("Disconnect in Telegram before switching Google accounts.")
                tokens.setdefault("refresh_token", active.get("refresh_token"))
            if not tokens.get("refresh_token"):
                raise GoogleUnavailable("Google offline access unavailable. Reconnect in Telegram.")
            confirmation = secrets.token_urlsafe(24)
            pending = {**tokens, "sub": identity["sub"], "email": identity["email"], "confirmation": confirmation}
            if "resume" in flow:
                pending["resume"] = flow["resume"]
            expires = min(self.store.clock() + 600, flow["resume"]["expires"]) if "resume" in flow else self.store.clock() + 600
            db.execute("UPDATE connections SET pending=?,pending_hash=?,pending_until=? WHERE chat=?",
                       (self.store.seal(row["chat"], row["generation"], "pending", pending), digest(confirmation),
                        expires, row["chat"]))

    def _tokens(self, value):
        if (not isinstance(value, dict) or not isinstance(value.get("access_token"), str)
                or not 1 <= len(value["access_token"]) <= 8192 or not isinstance(value.get("token_type"), str)
                or value["token_type"].lower() != "bearer"
                or type(value.get("expires_in")) is not int or not 0 < value["expires_in"] <= 86400
                or not isinstance(value.get("scope", ""), str)):
            raise GoogleUnavailable("Google authorization response was invalid.")
        if any(c in value["access_token"] for c in "\r\n"):
            raise GoogleUnavailable("Google authorization response was invalid.")
        result = {"access_token": value["access_token"], "expires_at": self.store.clock() + value["expires_in"],
                  "scopes": value.get("scope", "").split()}
        if "refresh_token" in value:
            if not isinstance(value["refresh_token"], str) or not 1 <= len(value["refresh_token"]) <= 8192:
                raise GoogleUnavailable("Google authorization response was invalid.")
            result["refresh_token"] = value["refresh_token"]
        return result

    def credentials(self, chat, deadline=None, *, run_guard=None):
        """Service-internal only. Never expose this result through HTTP or model tools."""
        validate_chat(chat)
        deadline = deadline or time.monotonic() + 20
        with bounded_lock(self.lifecycle_lock, deadline), bounded_lock(self.refresh_lock, deadline):
            if run_guard is not None:
                deadline = min(deadline, run_guard())
            with self.store.transaction(deadline) as db:
                row = self.store.connection(db, chat)
                if row["status"] != "active" or not row["active"]:
                    raise PermissionError("Google connection unavailable.")
                active = self.store.open(chat, row["generation"], "active", row["active"])
            if active["expires_at"] > self.store.clock() + 60:
                return row["generation"], active
            if run_guard is not None:
                deadline = min(deadline, run_guard())
            try:
                response = self.transport.request(TOKEN_URL, {"client_id": self.config.client_id,
                    "client_secret": self.config.client_secret, "refresh_token": active["refresh_token"],
                    "grant_type": "refresh_token"}, timeout=remaining(deadline))
                remaining(deadline)
                updated = self._tokens(response)
                if "scope" not in response:
                    updated["scopes"] = active["scopes"]
                active.update(updated)
            except GrantExpired:
                with self.store.transaction(deadline) as db:
                    changed = db.execute("UPDATE connections SET status='reconnect',active=NULL,pending=NULL,pending_hash=NULL,pending_until=NULL,generation=generation+1 WHERE chat=? AND generation=?",
                                         (chat, row["generation"]))
                    if changed.rowcount:
                        db.execute("DELETE FROM flows WHERE chat=? AND generation=?", (chat, row["generation"]))
                raise
            with self.store.transaction(deadline) as db:
                saved = db.execute("UPDATE connections SET active=? WHERE chat=? AND generation=? AND status='active'",
                                   (self.store.seal(chat, row["generation"], "active", active), chat, row["generation"]))
                if not saved.rowcount:
                    raise PermissionError("Google connection changed during request.")
            return row["generation"], active
