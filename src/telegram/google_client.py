"""Telegram-only Google controls. No model-facing configuration or credentials."""
import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class GoogleClient:
    def __init__(self, url, secret):
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise ValueError("Invalid Google service URL.")
        if len(secret) < 32 or not secret.isascii():
            raise ValueError("Invalid Google control secret.")
        self.url, self.secret = url.rstrip("/"), secret
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def command(self, chat, request_id, text):
        pieces = text.split()
        if pieces == ["/google"]:
            action, confirmation = "menu_google", None
        elif pieces == ["/calendars"]:
            action, confirmation = "menu_calendars", None
        elif len(pieces) == 2 and pieces[0] == "/google_callback":
            action, confirmation = "callback", pieces[1]
        elif len(pieces) == 2 and pieces[0] == "/google" and pieces[1] in ("connect", "disconnect"):
            action, confirmation = pieces[1], None
        elif len(pieces) == 3 and pieces[0] == "/google" and pieces[1] == "confirm":
            action, confirmation = "confirm", pieces[2]
        else:
            return "Google commands: /google, /google connect, /google disconnect."
        body = {"chat_id": str(chat), "request_id": str(request_id), "action": action}
        if confirmation:
            body["confirmation"] = confirmation
        request = Request(self.url + "/control", data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.secret})
        try:
            with self.opener.open(request, timeout=30) as response:
                encoded = response.read(16385)
            if len(encoded) > 16384:
                raise ValueError()
            result = json.loads(encoded)
            state = result["state"]
            if state == "menu":
                return {"google_control": True, "text": result["text"], "keyboard": result["keyboard"]}
            if state in ("disabled", "unavailable"):
                return "Google connection is not configured. Ask the deployment owner to check Google setup."
            if state == "login":
                warning = "Extra Google consent required. ALL access reset Off. Reopen settings and Save after account confirmation.\n" if result.get("reauthorization") else ""
                return (warning + f"Open {result['url']}\nSign in only on Google's page. Never send passwords or tokens here.\n"
                        "After login, send /google here to review and confirm the account. All data access stays off.")
            if result.get("pending"):
                pending = result["pending"]
                return (f"Google account waiting for confirmation: {pending['email']}\n"
                        "Confirm only if this is the account you intended to connect:\n"
                        f"/google confirm {pending['confirmation']}\nAll data access stays off after confirmation.")
            if action == "disconnect" or state == "disconnected" and "revoked" in result:
                if result.get("revoked") is True:
                    return "Google disconnected locally and Google access revoked."
                return ("Google disconnected locally. Google revocation could not be confirmed. "
                        "Remove Taskopotamus access at https://myaccount.google.com/connections.")
            if state == "active":
                return f"Google connected: {result['email']}\nGmail: Off. Calendar events: Off.\nSend /google or /calendars to choose access and Save."
            if state == "reconnect":
                return "Google authorization expired. All access is off. Reconnect with /google connect."
            return "Google is not connected. Send /google connect."
        except HTTPError as error:
            error.close()
            raise RuntimeError("Google request failed or expired. Send /google to check before retrying.") from None
        except (URLError, OSError, ValueError, KeyError, TypeError, HTTPException):
            raise RuntimeError("Google connection unavailable. Try /google again shortly.") from None
