import json
import os
import re
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from psycopg import Error as DatabaseError

from .agent_client import AgentClient
from .agent_client import AgentRequestError
from .delivery_store import DeliveryStore
from src.runtime import read_secret
from src.storage.database import Database


_activity_slot = threading.BoundedSemaphore(1)


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


def reply_for(message, allowed_users, agent=None, request_id=None, _structured=False):
    if not allowed_message(message, allowed_users):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    if text == "/start":
        return "Hi, I'm Taskopotamus. Send a message to chat, or /new to start fresh. Sign in with /codex_login; check login with /codex_status."
    if text == "/ping":
        return "pong"
    if text == "/delivery_status":
        if agent is None:
            return "Delivery status unavailable."
        items = agent.delivery_status(message["chat"]["id"])
        if not items:
            return "No recorded deliveries."
        states = {}
        for item in items:
            states[item["state"]] = states.get(item["state"], 0) + 1
        return "Recent delivery items: " + ", ".join(f"{state}: {count}" for state, count in sorted(states.items())) + ". Uncertain items are not automatically resent."
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
        return "Commands: /new, /ping, /codex_login, /codex_login retry, /codex_status, /delivery_status."
    if agent is None:
        return "Agent service is not configured."
    try:
        if text == "/new":
            return agent.reset(message["chat"]["id"])["text"]
        result = agent.chat(message["chat"]["id"], text, request_id)
        return result if _structured else result["text"]
    except RuntimeError as error:
        if _structured:
            raise
        return str(error)


def reply_chunks(text):
    # 2000 Unicode characters fit Telegram's limit even when every character uses two UTF-16 units.
    return [text[start:start + 2000] for start in range(0, len(text), 2000)]


class TelegramRejected(RuntimeError):
    """A definitive API rejection, rather than an ambiguous transport failure."""


def api(token, method, body=None, timeout=40):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except HTTPError as error:
        rejected = False
        try:
            body = error.read(65537) if error.fp is not None else b""
            if len(body) <= 65536:
                response = json.loads(body)
                rejected = isinstance(response, dict) and response.get("ok") is False and type(response.get("error_code")) is int
        except (ValueError, OSError):
            pass
        error.close()
        raise (TelegramRejected if rejected else RuntimeError)(f"Telegram {method} failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Telegram network request failed.") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("Telegram returned invalid JSON.") from None
    if isinstance(data, dict) and data.get("ok") is False and type(data.get("error_code")) is int:
        raise TelegramRejected(f"Telegram {method} rejected the request.")
    if not isinstance(data, dict) or not data.get("ok") or "result" not in data:
        raise RuntimeError(f"Telegram {method} failed.")
    return data["result"]


def process_update(update, allowed_users, agent, token, store, wake=None):
    update_id = update["update_id"]
    if update_id < store.offset:
        return
    message = update.get("message")
    if not allowed_message(message, allowed_users):
        store.finish(update_id)
        return
    pending = store.get(update_id)
    if pending is not None:
        chat_id, chunks, next_chunk = pending
        agent.import_legacy_delivery(chat_id, str(update_id), chunks, next_chunk)
    else:
        command_attempt = None
        known_reply = None
        if isinstance(message.get("text"), str) and message["text"].startswith("/"):
            reservation = agent.reserve_command(message["chat"]["id"], str(update_id), message["text"])
            known_reply, command_attempt = reservation["reply"], reservation["attempt"]
        elif store.uncertain(update_id):
            known_reply = agent.lookup_reply(message["chat"]["id"], str(update_id))
        if known_reply is not None:
            store.finish(update_id)
            if wake is not None:
                wake.set()
            return
        stopped = threading.Event()
        activity = None
        if isinstance(message.get("text"), str) and not message["text"].startswith("/") and _activity_slot.acquire(blocking=False):
            activity = threading.Thread(target=show_activity, args=(token, message["chat"]["id"], stopped), daemon=True)
            activity.start()
        try:
            try:
                result = reply_for(message, allowed_users, agent, request_id=str(update_id), _structured=True)
            except AgentRequestError as error:
                if error.status in (400, 401, 429, 507) and not store.uncertain(update_id):
                    result = str(error)
                else:
                    store.mark_uncertain(update_id)
                    raise
            except RuntimeError:
                store.mark_uncertain(update_id)
                raise
            if result is None:
                store.finish(update_id)
                return
            if isinstance(result, dict):
                if not isinstance(result.get("delivery_id"), str):
                    # An old cached or interrupted reply has no proof of prior delivery.
                    agent.import_legacy_delivery(message["chat"]["id"], str(update_id), reply_chunks(result["text"]), 0)
            else:
                store.mark_uncertain(update_id)
                if command_attempt is not None:
                    agent.publish_reply(message["chat"]["id"], str(update_id), result, command_attempt)
                else:
                    agent.publish_reply(message["chat"]["id"], str(update_id), result)
        finally:
            stopped.set()
            if activity is not None:
                activity.join(timeout=0.1)
    store.finish(update_id)
    if wake is not None:
        wake.set()


def show_activity(token, chat_id, stopped):
    try:
        while not stopped.is_set():
            try:
                api(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=3)
            except RuntimeError:
                pass
            stopped.wait(4)
    finally:
        _activity_slot.release()


def deliver_one(agent, token, allowed_users):
    delivery = agent.claim_delivery(allowed_users)
    if delivery is None:
        return False
    state, receipt = "uncertain", None
    try:
        item = delivery["item"]
        if item.get("type") != "text":
            state = "failed"
        else:
            result = api(token, "sendMessage", {
                "chat_id": delivery["chat_id"], "text": item["text"], "protect_content": True,
                "link_preview_options": {"is_disabled": True},
            })
            if isinstance(result, dict) and type(result.get("message_id")) is int and result["message_id"] > 0:
                state, receipt = "delivered", {"message_id": result["message_id"]}
    except TelegramRejected:
        state = "failed"
    except RuntimeError:
        pass
    # Retry only recording the same receipt. Never repeat the Telegram send here.
    for attempt in range(3):
        try:
            agent.acknowledge_delivery(delivery, state, receipt)
            return True
        except RuntimeError:
            if attempt == 2:
                raise
    return True


def delivery_loop(agent, token, allowed_users, wake, stopped):
    while not stopped.is_set():
        try:
            if deliver_one(agent, token, allowed_users):
                continue
        except (RuntimeError, KeyError, TypeError):
            print("Delivery unavailable; unresolved sends will not be repeated.", file=sys.stderr, flush=True)
        wake.wait(1)
        wake.clear()


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
    wake, stopped = threading.Event(), threading.Event()
    sender = threading.Thread(target=delivery_loop, args=(agent, token, allowed_users, wake, stopped), daemon=True)
    sender.start()
    try:
        while True:
            try:
                updates = api(token, "getUpdates", {
                    "offset": store.offset, "timeout": 25, "allowed_updates": ["message"],
                })
                for update in updates:
                    process_update(update, allowed_users, agent, token, store, wake)
            except (RuntimeError, DatabaseError) as error:
                message = str(error) if isinstance(error, RuntimeError) else "Telegram storage unavailable. Retrying."
                print(message, file=sys.stderr, flush=True)
                time.sleep(5)
    finally:
        stopped.set()
        wake.set()
        sender.join(timeout=2)


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
