import json
import os
import re
import sys
import threading
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from psycopg import Error as DatabaseError

from .agent_client import AgentClient
from .agent_client import AgentRequestError
from .media import TelegramRejected, rejection, send_media
from .inbox import Inbox
from .input_files import InputRejected, download
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


def api(token, method, body=None, timeout=40):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if method == "sendMessage":
                encoded = response.read(65537)
                if len(encoded) > 65536:
                    raise RuntimeError("Telegram returned an excessive response.")
                data = json.loads(encoded)
            else:
                data = json.load(response)
    except HTTPError as error:
        rejected = None
        try:
            body = error.read(65537) if error.fp is not None else b""
            if len(body) <= 65536:
                response = json.loads(body)
                if isinstance(response, dict) and response.get("error_code") == error.code:
                    rejected = rejection(response)
        except (ValueError, OSError, HTTPException):
            pass
        error.close()
        raise rejected or RuntimeError(f"Telegram {method} failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise RuntimeError("Telegram network request failed.") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("Telegram returned invalid JSON.") from None
    if isinstance(data, dict) and data.get("ok") is False and type(data.get("error_code")) is int:
        raise rejection(data)
    if not isinstance(data, dict) or not data.get("ok") or "result" not in data:
        raise RuntimeError(f"Telegram {method} failed.")
    return data["result"]


def process_update(update, allowed_users, agent, token, store, wake=None, persisted=False):
    update_id = update["update_id"]
    if not persisted and update_id < store.offset:
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
        previously_uncertain = store.uncertain(update_id)
        if isinstance(message.get("text"), str) and message["text"].startswith("/"):
            reservation = agent.reserve_command(message["chat"]["id"], str(update_id), message["text"])
            known_reply, command_attempt = reservation["reply"], reservation["attempt"]
        elif previously_uncertain:
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
                if isinstance(message.get("text"), str) and not message["text"].startswith("/"):
                    # Persist intent before forwarding: a process crash may bypass every error handler.
                    store.mark_uncertain(update_id)
                result = reply_for(message, allowed_users, agent, request_id=str(update_id), _structured=True)
            except AgentRequestError as error:
                if error.status in (400, 401, 429, 507) and not previously_uncertain:
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
        if item.get("type") == "text":
            body = {
                "chat_id": delivery["chat_id"], "text": item["text"], "protect_content": True,
                "link_preview_options": {"is_disabled": True},
            }
            if item.get("parse_mode") is not None:
                body["parse_mode"] = item["parse_mode"]
            result = api(token, "sendMessage", body)
        elif item.get("type") in ("image", "file"):
            info = delivery.get("artifact") or agent.artifact_info(delivery["chat_id"], item["artifact_id"])
            with agent.artifact_stream(delivery["chat_id"], item["artifact_id"]) as source:
                result = send_media(token, delivery["chat_id"], item, info, source)
        else:
            raise RuntimeError("Unsupported outgoing item.")
        if isinstance(result, dict) and type(result.get("message_id")) is int and result["message_id"] > 0:
            state, receipt = "delivered", {"message_id": result["message_id"]}
            media = result.get("document") if item["type"] == "file" else (result.get("photo") or [{}])[-1] if item["type"] == "image" else None
            if isinstance(media, dict) and isinstance(media.get("file_id"), str):
                receipt["file_id"] = media["file_id"]
    except TelegramRejected as error:
        state, receipt = ("rate_limited", {"retry_after": error.retry_after}) if error.retry_after is not None else ("failed", None)
        if error.formatting and item.get("type") == "text" and item.get("parse_mode") == "HTML":
            state, receipt = "formatting_rejected", None
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


def process_input(job, inbox, allowed_users, agent, token, store, wake):
    identifier, chat = job["id"], job["chat_id"]
    if chat not in allowed_users:
        inbox.finish(identifier)
        return
    messages = job["messages"]
    files = [file for message in messages for file in message["files"]]
    text = "\n".join(message["text"] for message in messages if message["text"])
    if len(messages) > 1:
        text = "\n".join(
            f'Attachment {index+1}: {message["text"]}' for index, message in enumerate(messages) if message["text"]
        )
    if not files and not job.get("error"):
        update = {
            "update_id": identifier,
            "message": {"chat": {"id": int(chat), "type": "private"}, "from": {"id": int(chat)}, "text": text},
        }
        process_update(update, allowed_users, agent, token, store, wake, persisted=True)
        inbox.finish(identifier)
        return
    stopped = threading.Event()
    activity = None
    if _activity_slot.acquire(blocking=False):
        activity = threading.Thread(target=show_activity, args=(token, int(chat), stopped), daemon=True)
        activity.start()
    inference = False
    try:
        if agent.lookup_reply(chat, str(identifier)) is not None:
            inbox.finish(identifier)
            wake.set()
            return
        if job.get("error") or any(message.get("error") for message in messages):
            raise InputRejected(
                job.get("error") or next(message["error"] for message in messages if message.get("error"))
            )
        if len(text) > 16000 or len(files) > 10:
            raise InputRejected("Maximum 16000 text characters, ten files and 20 MB combined.")
        accepted = []
        total = 0
        for index, descriptor in enumerate(files):
            info = job.get("uploads", {}).get(str(index)) or agent.input_lookup(chat, str(identifier), str(index))
            if info is None:
                meta, stream = download(token, descriptor, api, remaining=20000000 - total)
                with stream:
                    info = agent.upload_input(chat, str(identifier), str(index), meta, stream)
                inbox.save_upload(identifier, index, info)
            total += info["size"]
            if total > 20000000:
                raise InputRejected("Files exceed 20 MB combined.")
            accepted.append(info["artifact_id"])
        inbox.mark_inference(identifier)
        inference = True
        result = agent.chat(int(chat), text, str(identifier), accepted)
        if not isinstance(result.get("delivery_id"), str):
            agent.import_legacy_delivery(chat, str(identifier), reply_chunks(result["text"]), 0)
        inbox.finish(identifier)
        wake.set()
    except InputRejected as error:
        agent.publish_reply(chat, str(identifier), str(error))
        inbox.finish(identifier)
        wake.set()
    except AgentRequestError as error:
        if error.status in (400, 401, 403, 507) and not job.get("inference_started"):
            agent.publish_reply(chat, str(identifier), str(error))
            inbox.finish(identifier)
            wake.set()
        else:
            inbox.finish(identifier, retry=True)
            raise
    except RuntimeError:
        if not inference and inbox.failed_transfer(identifier) >= 3:
            agent.publish_reply(
                chat, str(identifier), "File transfer failed. No agent work started; please send the files again."
            )
            inbox.finish(identifier)
            wake.set()
        else:
            inbox.finish(identifier, retry=True)
            raise
    finally:
        stopped.set()
        if activity is not None:
            activity.join(timeout=0.1)


def input_loop(inbox, agent, token, allowed, store, wake, stopped):
    with inbox.database.connect() as connection:
        owns = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('telegram-input-dispatcher',0))"
        ).fetchone()[0]
        connection.commit()
        if not owns:
            return
        inbox.recover()
        dispatch_inputs(inbox, agent, token, allowed, store, wake, stopped)


def dispatch_inputs(inbox, agent, token, allowed, store, wake, stopped):
    while not stopped.is_set():
        job = None
        try:
            job = inbox.claim()
            if job is not None:
                process_input(job, inbox, allowed, agent, token, store, wake)
                continue
        except Exception:
            if job is not None:
                inbox.finish(job["id"], retry=True)
            print("Input processing unavailable; request identity is preserved.", file=sys.stderr, flush=True)
        stopped.wait(1)


def main():
    os.umask(0o077)
    token, allowed_users = read_config(os.environ)
    agent = AgentClient(os.environ.get("AGENT_URL", "http://agent:8080"), read_secret("AGENT_API_TOKEN"))
    store = DeliveryStore(Database.from_env())
    inbox = Inbox(store.database)
    api(token, "getMe")
    webhook = api(token, "getWebhookInfo")
    if webhook.get("url"):
        raise RuntimeError("Webhook is enabled. Remove it before using long polling.")
    with store.database.connect() as db:
        legacy = db.execute("SELECT update_id,chat_id,chunks,next_chunk FROM pending ORDER BY update_id").fetchall()
    for identifier, chat, chunks, next_chunk in legacy:
        if str(chat) in allowed_users:
            agent.import_legacy_delivery(chat, str(identifier), json.loads(chunks), next_chunk)
        store.finish(identifier)
    print("Telegram connected. Ready for private messages from allowed users.", flush=True)
    wake, stopped = threading.Event(), threading.Event()
    sender = threading.Thread(target=delivery_loop, args=(agent, token, allowed_users, wake, stopped), daemon=True)
    sender.start()
    dispatcher = threading.Thread(
        target=input_loop, args=(inbox, agent, token, allowed_users, store, wake, stopped), daemon=True
    )
    dispatcher.start()
    try:
        while True:
            try:
                updates = api(
                    token,
                    "getUpdates",
                    {
                        "offset": inbox.offset,
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    },
                )
                for update in updates:
                    inbox.receive(update, allowed_users)
            except (RuntimeError, DatabaseError, BlockingIOError) as error:
                message = str(error) if isinstance(error, RuntimeError) else "Telegram storage unavailable. Retrying."
                print(message, file=sys.stderr, flush=True)
                time.sleep(5)
    finally:
        stopped.set()
        wake.set()
        sender.join(timeout=2)
        dispatcher.join(timeout=2)


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
