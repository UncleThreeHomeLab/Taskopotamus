import json
import os
import re
import sys
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from psycopg import Error as DatabaseError

from .agent_client import AgentClient
from .delivery_store import DeliveryStore
from .formatting import format_reply
from src.runtime import read_secret
from src.storage.database import Database


def read_config(env):
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    ids = [value.strip() for value in env.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")]
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Set TELEGRAM_BOT_TOKEN to your BotFather token.")
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in ids):
        raise ValueError("Set TELEGRAM_ALLOWED_USER_IDS to comma-separated numeric user IDs.")
    return token, set(ids)


def allowed_message(message, allowed_users):
    return (
        isinstance(message, dict)
        and isinstance(message.get("chat"), dict)
        and message["chat"].get("type") == "private"
        and isinstance(message.get("from"), dict)
        and str(message["from"].get("id")) in allowed_users
    )


def reply_for(message, allowed_users, agent=None, request_id=None):
    if not allowed_message(message, allowed_users):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    if text == "/start":
        return "Hi, I'm Taskopotamus. Send a message to chat, or /new to start fresh. Sign in with /codex_login; check login with /codex_status."
    if text == "/ping":
        return "pong"
    if text in ("/codex_login", "/codex_login retry", "/codex_status"):
        if agent is None:
            return "Agent service is not configured."
        try:
            if text == "/codex_login retry":
                result = agent.login(restart=True)
            else:
                result = agent.login() if text == "/codex_login" else agent.status()
        except RuntimeError:
            return "Codex login unavailable. Check agent service and try again."
        if result["state"] == "signed_in":
            return "Codex is signed in."
        if result["state"] == "pending" and "user_code" in result:
            return (
                f"Open {result['verification_url']}\n"
                f"Enter code: {result['user_code']}\n"
                "Sign in only on the OpenAI page. Never send passwords or tokens here.\n"
                "Then send /codex_status to confirm.\n"
                "If the code is rejected, send /codex_login retry for a fresh code."
            )
        if result["state"] == "pending":
            return "Codex login pending. Finish signing in through your browser."
        if result["state"] == "failed":
            return "Codex login failed or expired. Enable device code login in ChatGPT settings, then retry /codex_login."
        return "Codex is signed out. Send /codex_login."
    if text.startswith("/") and text != "/new":
        return "Commands: /new, /ping, /codex_login, /codex_login retry, /codex_status."
    if agent is None:
        return "Agent service is not configured."
    try:
        if text == "/new":
            return agent.reset(message["chat"]["id"])["text"]
        return agent.chat(message["chat"]["id"], text, request_id)["text"]
    except RuntimeError as error:
        return str(error)


class TelegramSendError(RuntimeError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__("Telegram send failed." if kind != "uncertain" else "Telegram delivery outcome uncertain.")


def _send_rejection(data, body, http_code=None):
    if not isinstance(data, dict) or data.get("ok") is not False or type(data.get("error_code")) is not int:
        return "uncertain"
    code = data["error_code"]
    if http_code is not None and code != http_code:
        return "uncertain"
    description = data.get("description", "")
    if code == 400 and body.get("parse_mode") == "HTML" and isinstance(description, str) and description.lower().startswith("bad request: can't parse entities"):
        return "formatting"
    if code == 429:
        return "transient"
    return "rejected" if 400 <= code < 500 else "uncertain"


def api(token, method, body=None):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=40) as response:
            if method == "sendMessage":
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise ValueError("Oversized send response")
                data = json.loads(raw)
            else:
                data = json.load(response)
    except HTTPError as error:
        if method == "sendMessage":
            try:
                raw = error.read(65537)
                data = json.loads(raw) if len(raw) <= 65536 else None
            except (OSError, HTTPException, ValueError, UnicodeError):
                data = None
            finally:
                error.close()
            raise TelegramSendError(_send_rejection(data, body or {}, error.code)) from None
        error.close()
        raise RuntimeError(f"Telegram {method} failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        if method == "sendMessage":
            raise TelegramSendError("uncertain") from None
        raise RuntimeError("Telegram network request failed.") from None
    except (ValueError, UnicodeError):
        if method == "sendMessage":
            raise TelegramSendError("uncertain") from None
        raise RuntimeError("Telegram returned invalid JSON.") from None
    if method == "sendMessage":
        if isinstance(data, dict) and data.get("ok") is False:
            raise TelegramSendError(_send_rejection(data, body or {}))
        if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("result"), dict) or type(data["result"].get("message_id")) is not int:
            raise TelegramSendError("uncertain")
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed.")
    return data["result"]


def process_update(update, allowed_users, agent, token, store):
    update_id = update["update_id"]
    if update_id < store.offset:
        return
    message = update.get("message")
    if not allowed_message(message, allowed_users):
        store.finish(update_id)
        return
    pending = store.get(update_id)
    if pending is None:
        text = reply_for(message, allowed_users, agent, request_id=str(update_id))
        if text is None:
            store.finish(update_id)
            return
        chat_id = message["chat"]["id"]
        chunks = format_reply(text)
        if not chunks:
            store.finish(update_id)
            return
        store.prepare(update_id, chat_id, chunks)
        pending = (chat_id, chunks, 0)
    chat_id, chunks, next_chunk = pending
    for index in range(next_chunk, len(chunks)):
        chunk = chunks[index]
        if chunk["state"] != "pending":
            state = "rejected" if chunk["state"] == "rejected" else "uncertain"
            store.quarantine(update_id, index, state, "telegram_rejected" if state == "rejected" else "recovered_inflight")
            print(f"Telegram delivery quarantined ({state}).", file=sys.stderr, flush=True)
            return
        while True:
            store.mark_sending(update_id, index, chunk)
            body = {"chat_id": chat_id, "text": chunk["text"], "protect_content": True,
                    "link_preview_options": {"is_disabled": True}}
            if chunk["parse_mode"] is not None:
                body["parse_mode"] = chunk["parse_mode"]
            try:
                api(token, "sendMessage", body)
            except RuntimeError as error:
                kind = error.kind if isinstance(error, TelegramSendError) else "uncertain"
                if kind == "formatting" and chunk["parse_mode"] == "HTML":
                    chunk = {**chunk, "text": chunk["plain_text"], "parse_mode": None, "state": "pending"}
                    store.replace_chunk(update_id, index, chunk)
                    continue
                if kind == "transient":
                    store.replace_chunk(update_id, index, {**chunk, "state": "pending"})
                    raise
                state = "rejected" if kind in ("rejected", "formatting") else "uncertain"
                store.quarantine(update_id, index, state, "telegram_rejected" if state == "rejected" else "network_outcome_unknown")
                print(f"Telegram delivery quarantined ({state}).", file=sys.stderr, flush=True)
                return
            break
        store.mark_sent(update_id, index + 1)
    store.finish(update_id)


def main():
    os.umask(0o077)
    token, allowed_users = read_config(os.environ)
    agent = AgentClient(os.environ.get("AGENT_URL", "http://agent:8080"), read_secret("AGENT_API_TOKEN"))
    store = DeliveryStore(Database.from_env())
    api(token, "getMe")
    webhook = api(token, "getWebhookInfo")
    if webhook.get("url"):
        raise RuntimeError("Webhook is enabled. Remove it before using long polling.")
    print("Telegram connected. Ready for private messages from allowed users.", flush=True)
    while True:
        try:
            updates = api(token, "getUpdates", {
                "offset": store.offset, "timeout": 25, "allowed_updates": ["message"],
            })
            for update in updates:
                process_update(update, allowed_users, agent, token, store)
        except (RuntimeError, DatabaseError) as error:
            message = str(error) if isinstance(error, RuntimeError) else "Telegram storage unavailable. Retrying."
            print(message, file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Telegram failed to start. Check configuration and storage.", file=sys.stderr)
        sys.exit(1)
