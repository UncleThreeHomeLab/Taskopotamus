"""Server-rendered local console. All runtime and user strings remain plain text."""
from html import escape
import json
import re


def render(state):
    def form(action, label):
        disabled = " disabled" if state["pending"] and action != "status" else ""
        return (f'<form method="post" action="/{action}"><input type="hidden" name="csrf" value="{escape(state["csrf"])}">'
                f'<input type="hidden" name="nonce" value="{state["nonce"]}"><button{disabled}>{label}</button></form>')

    def content(value):
        if isinstance(value, str):
            return f'<pre>{escape(value)}</pre>'
        pieces = []
        for item in value:
            if item.get("type") == "text":
                pieces.append(f'<pre>{escape(item["text"])}</pre>')
            elif item.get("type") in ("image", "file"):
                info = item.get("metadata", {})
                caption = escape(item.get("caption", ""))
                identifier = item.get("artifact_id", "")
                filename = escape(info.get("filename", "Attachment unavailable or expired"))
                if (info.get("state") != "committed" or not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier)
                        or type(info.get("size")) is not int or info["size"] <= 0):
                    pieces.append(f'<p>{filename}</p><pre>{caption}</pre>')
                    continue
                preview = f'<img src="/artifact/{identifier}" alt="{filename}" style="max-width:100%;height:auto">' if item["type"] == "image" and info.get("presentation_mime") in ("image/png", "image/jpeg") else ""
                pieces.append(f'<figure>{preview}<figcaption><pre>{caption}</pre>'
                              f'<a href="/artifact/{identifier}/download">Download {filename}</a> ({info["size"]} bytes)</figcaption></figure>')
        return "".join(pieces)

    transcript = "".join(f'<article><strong>{escape(role)}</strong>{content(text)}</article>'
                         for role, text in state["transcript"])
    refresh = '<meta http-equiv="refresh" content="2">' if state["pending"] else ""
    auth = state["auth"]
    signin = ""
    if auth.get("verification_url") and auth.get("user_code"):
        signin = (f'<p>Continue only if you started this sign-in. Never share this code.</p>'
                  f'<a href="https://auth.openai.com/codex/device" target="_blank" rel="noopener noreferrer">Open OpenAI sign-in</a>'
                  f'<pre>{escape(auth["user_code"])}</pre>')
    details = state["details"]
    evidence = escape(json.dumps(details, indent=2, ensure_ascii=False)) if details else "Run details unavailable."
    replay = form("replay", "Check same request (never resend as new work)") if state["uncertain"] else ""
    disabled = " disabled" if state["pending"] else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}<title>Taskopotamus local console</title>
<style>body{{font:16px system-ui;max-width:850px;margin:2rem auto;padding:0 1rem;color:#162232;background:#f4f6fa}}
article,section{{background:white;border:1px solid #cbd2de;border-radius:8px;padding:1rem;margin:1rem 0}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}}textarea{{width:100%;box-sizing:border-box;font:inherit}}
button{{padding:.6rem .9rem;cursor:pointer}}nav{{display:flex;flex-wrap:wrap;gap:.5rem}}label{{display:block;font-weight:bold}}</style></head>
<body><h1>Taskopotamus local console</h1><p>Manual live testing. Sending messages consumes real model usage.</p>
<p>Separate console conversation and profile. Codex account, worker files, and busy slot are shared with Telegram. Trusted operators only.</p>
<p>Visible transcript clears when this console restarts; the agent conversation continues until New conversation.</p>
<section><h2>Codex sign-in</h2><p>Status: {escape(auth.get("state", "unknown"))}</p>{signin}
<nav>{form("status", "Refresh sign-in status")}{form("login", "Sign in")}{form("retry", "Retry sign-in")}{form("reset", "New conversation")}</nav></section>
<p role="status">{escape(state["notice"])}</p>{replay}<div aria-live="polite">{transcript}</div>
<form method="post" action="/chat"><input type="hidden" name="csrf" value="{escape(state["csrf"])}">
<input type="hidden" name="nonce" value="{state["nonce"]}"><label for="message">Message</label>
<textarea id="message" name="text" rows="5" maxlength="16000" required{disabled}>{escape(state["draft"])}</textarea>
<p><button{disabled}>Send message</button></p></form>
<section><h2>Latest run evidence</h2><p>Configured and requested models are not actual execution telemetry.
Command exit codes do not independently verify tests. Child commands may be unavailable in the parent result.</p><pre>{evidence}</pre></section>
</body></html>'''
