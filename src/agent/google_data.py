"""Capability-scoped Gmail gateway. Artifact DB locks never span outbound HTTP."""
import secrets
import threading
import time

from src.google_integration.data_protocol import authorization, body, capability, private_post, arguments, NoRedirects, remaining
from src.profiles.auth import ProfileTokens
from urllib.request import ProxyHandler, build_opener


class GoogleDataAPI:
    def __init__(self, artifacts, secret):
        self.artifacts, self.secret = artifacts, secret
        self.opener = build_opener(ProxyHandler({}), NoRedirects())
        # Leave agent HTTP threads available for trusted run-check callbacks.
        self.slots = threading.BoundedSemaphore(2)

    def check(self, token, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 10
        remaining(deadline)
        capability(token)
        chat, request = self.artifacts.scope(token)
        remaining(deadline)
        ProfileTokens.validate_telegram_chat(chat)
        with self.artifacts.database.connect() as db:
            if self.artifacts.authorize(db, token) != (chat, request):
                raise PermissionError()
            end = self.artifacts.active_run(db, chat, request)
        lifetime = end - self.artifacts.clock()
        remaining(deadline)
        if lifetime <= 0:
            raise PermissionError()
        return {"chat_id": chat, "request_id": request, "remaining_seconds": min(lifetime, 1800)}

    def dispatch(self, handler):
        if not handler.path.startswith("/google/"):
            return False
        acquired = False
        try:
            token = authorization(handler)
            if handler.command != "POST":
                raise PermissionError()
            if handler.path == "/google/run-check":
                if not secrets.compare_digest(token.encode(), self.secret.encode()):
                    raise PermissionError()
                payload = body(handler, 4096)
                if set(payload) != {"capability"}:
                    raise ValueError()
                result = self.check(payload["capability"])
            elif handler.path == "/google/worker/gmail":
                started = time.monotonic()
                initial = self.check(token, started + 20)
                deadline = started + min(20, initial["remaining_seconds"])
                payload = arguments(body(handler))
                acquired = self.slots.acquire(blocking=False)
                if not acquired:
                    handler.send_json({"error": "Google data busy."}, 429)
                    return True
                handler.read_deadline.cancel()
                result = private_post(self.opener, "http://google:8082/data/gmail", self.secret,
                                      {"capability": token, "arguments": payload},
                                      deadline)
                final = self.check(token, deadline)
                if (initial["chat_id"], initial["request_id"]) != (final["chat_id"], final["request_id"]):
                    raise PermissionError()
            else:
                raise PermissionError()
            handler.send_json(result, 200)
        except PermissionError:
            handler.send_json({"error": "Google data access denied."}, 403)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            handler.send_json({"error": "Invalid Google data request."}, 400)
        except Exception:
            handler.send_json({"error": "Google data unavailable."}, 503)
        finally:
            if acquired:
                self.slots.release()
        return True
