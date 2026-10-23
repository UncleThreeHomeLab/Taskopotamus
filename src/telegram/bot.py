import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from psycopg import Error as DatabaseError

from .agent_client import AgentClient
from .delivery_store import DeliveryStore
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


def reply_chunks(text):
    # 2000 Unicode characters fit Telegram's limit even when every character uses two UTF-16 units.
    return [text[start:start + 2000] for start in range(0, len(text), 2000)]


def api(token, method, body=None):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=40) as response:
            data = json.load(response)
    except HTTPError as error:
        error.close()
        raise RuntimeError(f"Telegram {method} failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Telegram network request failed.") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("Telegram returned invalid JSON.") from None
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
        chunks = reply_chunks(text)
        store.prepare(update_id, chat_id, chunks)
        pending = (chat_id, chunks, 0)
    chat_id, chunks, next_chunk = pending
    for index in range(next_chunk, len(chunks)):
        api(token, "sendMessage", {
            "chat_id": chat_id, "text": chunks[index], "protect_content": True,
            "link_preview_options": {"is_disabled": True},
        })
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
