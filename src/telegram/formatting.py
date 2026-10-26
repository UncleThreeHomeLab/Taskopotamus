"""Render untrusted Markdown into bounded Telegram HTML and plain alternatives."""
from dataclasses import dataclass, replace
from html import escape, unescape
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

import mistune


LIMIT = 4096
MAX_ENTITIES = 100
MAX_FORMATTED_CHUNKS = 128
MAX_MARKDOWN_SOURCE = 65536
MAX_RENDERED_TEXT = 262144


class ChatInlineParser(mistune.InlineParser):
    def finalize_tokens(self, tokens, depth):
        if depth >= self.max_nested_level:
            return super().finalize_tokens(tokens, depth)
        # Relax only CJK-adjacent strong closers; escaped text and parsed code stay opaque.
        parts, source, markers = [], [], []
        offset = 0
        for token in tokens:
            raw = token.get("raw", "")
            if token["type"] == "text" and token.get("_emphasis", True):
                start = 0
                for match in re.finditer(r"(?<!\*)\*\*(?!\*)", raw):
                    if match.start() > start:
                        parts.append({**token, "raw": raw[start:match.start()]})
                    markers.append((len(parts), offset + match.start()))
                    parts.append({**token, "raw": "**"})
                    start = match.end()
                if start < len(raw):
                    parts.append({**token, "raw": raw[start:]})
                source.append(raw)
                offset += len(raw)
            else:
                parts.append(token)
                source.append(raw if token["type"] == "text" else "\ufffc")
                offset += len(source[-1])
        text = "".join(source)
        pairs, opener = {}, None
        for index, pos in markers:
            previous = text[pos - 1] if pos else "\n"
            following = text[pos + 2] if pos + 2 < len(text) else "\n"
            previous_punctuation = not previous.isspace() and not previous.isalnum()
            following_punctuation = not following.isspace() and not following.isalnum()
            can_open = not following.isspace() and (not following_punctuation or previous.isspace() or previous_punctuation)
            can_close = not previous.isspace() and (not previous_punctuation or following.isspace() or following_punctuation)
            cjk_close = previous_punctuation and following.isalnum() and unicodedata.east_asian_width(following) in ("W", "F")
            if opener is not None and (can_close or cjk_close):
                if cjk_close and not can_close:
                    pairs[opener] = index
                opener = None
            elif can_open:
                opener = index
        result, index = [], 0
        while index < len(parts):
            end = pairs.get(index)
            if end is None:
                result.append(parts[index])
                index += 1
            else:
                children = super().finalize_tokens(parts[index + 1:end], depth + 1)
                result.append({"type": "strong", "children": children})
                index = end + 1
        return super().finalize_tokens(result, depth)


def units(text):
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class Run:
    text: str
    marks: tuple = ()
    href: str | None = None
    quote: bool = False
    code: str | None = None
    language: str | None = None
    separator: bool = False


def safe_url(value):
    if not isinstance(value, str) or any(char.isspace() or unicodedata.category(char) in ("Cc", "Cs") or char == "\\" for char in value):
        return False
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
            return False
        host = parts.hostname
        if parts.netloc.startswith("["):
            if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parts.netloc) or "%" in host:
                return False
            ipaddress.IPv6Address(host)
        else:
            if not re.fullmatch(r"[^\[\]:]+(?::[0-9]+)?", parts.netloc):
                return False
            encoded = host.encode("idna").decode("ascii").rstrip(".")
            if re.fullmatch(r"[0-9.]+", encoded):
                ipaddress.IPv4Address(encoded)
            elif len(encoded) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in encoded.split(".")):
                return False
        return parts.port is None or 0 < parts.port < 65536
    except (ValueError, UnicodeError):
        return False


class Renderer:
    def __init__(self, budget):
        self.runs = []
        self.emitted = 0
        self.budget = budget

    def emit(self, text, **context):
        if not text:
            return
        if self.emitted + len(text) > self.budget:
            raise ValueError("Markdown expansion limit")
        self.emitted += len(text)
        run = Run(text, **context)
        if self.runs and self.runs[-1].separator and run.separator:
            self.runs[-1] = replace(run, text="\n\n")
            return
        if self.runs and len(self.runs[-1].text) + len(text) <= LIMIT and replace(self.runs[-1], text=text) == run:
            self.runs[-1] = replace(run, text=self.runs[-1].text + text)
        else:
            self.runs.append(run)

    def inline(self, nodes, marks=(), quote=False, href=None, depth=0):
        if depth > 64:
            raise ValueError("Markdown nesting limit")
        for node in nodes:
            kind = node["type"]
            children = node.get("children", [])
            if kind in ("text", "inline_html"):
                text = node["raw"] if kind == "inline_html" else unescape(node["raw"])
                self.emit(text, marks=marks, quote=quote, href=href)
            elif kind in ("softbreak", "linebreak"):
                self.emit("\n", marks=marks, quote=quote, href=href)
            elif kind in ("strong", "emphasis", "strikethrough"):
                mark = {"strong": "b", "emphasis": "i", "strikethrough": "s"}[kind]
                self.inline(children, tuple(dict.fromkeys((*marks, mark))), quote, href, depth + 1)
            elif kind == "codespan":
                # Code cannot overlap Telegram's other entity types.
                self.emit(node["raw"], code="inline")
            elif kind in ("link", "image"):
                if href is not None:
                    raise ValueError("Nested Markdown links")
                url = node.get("attrs", {}).get("url", "")
                if not isinstance(url, str):
                    raise ValueError("Invalid destination")
                simple = not any(child["type"] in ("codespan", "image", "link") for child in _descendants(children))
                clickable = kind == "link" and simple and safe_url(url) and units(url) <= LIMIT // 2 - 8
                if clickable:
                    start = self.emitted
                    self.inline(children, marks, quote, url, depth + 1)
                    if self.emitted == start:
                        self.emit(url, marks=marks, quote=quote, href=url)
                else:
                    self.inline(children, marks, quote, None, depth + 1)
                    self.emit(f" ({url})" if children else url, marks=marks, quote=quote)
            else:
                raise ValueError("Unsupported Markdown node")

    def blocks(self, nodes, quote_depth=0, indent=0, depth=0):
        if depth > 64:
            raise ValueError("Markdown nesting limit")
        quoted = quote_depth > 0
        for node in nodes:
            kind = node["type"]
            children = node.get("children", [])
            if kind in ("paragraph", "block_text", "heading"):
                if quote_depth > 1:
                    self.emit("> " * (quote_depth - 1), quote=True)
                self.inline(children, ("b",) if kind == "heading" else (), quoted)
                self.emit("\n" if kind == "block_text" else "\n\n", quote=quoted, separator=True)
            elif kind in ("blank_line", "thematic_break"):
                if kind == "thematic_break":
                    self.emit("\n\n", quote=quoted, separator=True)
            elif kind == "block_code":
                info = node.get("attrs", {}).get("info", "")
                language = info if isinstance(info, str) and re.fullmatch(r"[A-Za-z0-9_+.-]{1,40}", info) else None
                if quoted:
                    self.emit("> " * quote_depth)
                self.emit(node["raw"], code="block", language=language)
                self.emit("\n\n", separator=True)
            elif kind == "block_html":
                self.emit(node["raw"], quote=quoted)
                self.emit("\n\n", quote=quoted, separator=True)
            elif kind == "block_quote":
                self.blocks(children, quote_depth + 1, indent, depth + 1)
                self.emit("\n\n", quote=quoted, separator=True)
            elif kind == "list":
                attrs = node.get("attrs", {})
                start = attrs.get("start", 1)
                for number, item in enumerate(children, start):
                    prefix = f"{number}. " if attrs.get("ordered") else "• "
                    if item["type"] == "task_list_item":
                        prefix += "[x] " if item.get("attrs", {}).get("checked") else "[ ] "
                    elif item["type"] != "list_item":
                        raise ValueError("Unsupported list item")
                    self.emit("  " * indent + prefix, quote=quoted)
                    self.blocks(item.get("children", []), quote_depth, indent + 1, depth + 1)
                    if self.runs and self.runs[-1].separator:
                        self.runs[-1] = replace(self.runs[-1], text="\n")
                self.emit("\n", quote=quoted, separator=True)
            elif kind == "table":
                self.table(children, quoted)
            else:
                raise ValueError("Unsupported Markdown block")

    def table(self, nodes, quoted):
        if len(nodes) != 2 or nodes[0]["type"] != "table_head" or nodes[1]["type"] != "table_body":
            raise ValueError("Unsupported table")
        headers = nodes[0].get("children", [])
        rows = nodes[1].get("children", [])
        for row in rows or [{"children": [{"children": []} for _ in headers]}]:
            cells = row.get("children", [])
            if len(cells) != len(headers):
                raise ValueError("Uneven table")
            for position, (header, cell) in enumerate(zip(headers, cells), 1):
                before = self.emitted
                self.inline(header.get("children", []), ("b",), quoted)
                if self.emitted == before:
                    self.emit(f"Column {position}", marks=("b",), quote=quoted)
                self.emit(": ", quote=quoted)
                self.inline(cell.get("children", []), quote=quoted)
                self.emit("\n", quote=quoted, separator=True)
            self.emit("\n", quote=quoted, separator=True)


def clusters(text):
    """Keep common emoji/combining sequences together without adding a grapheme dependency."""
    cluster = ""
    regional_count = 0
    for char in text:
        number = ord(char)
        regional = 0x1F1E6 <= number <= 0x1F1FF
        extension = unicodedata.category(char).startswith("M") or 0xFE00 <= number <= 0xFE0F or 0x1F3FB <= number <= 0x1F3FF or 0xE0020 <= number <= 0xE007F
        if cluster and not (extension or char == "\u200d" or cluster.endswith("\u200d") or (regional and regional_count == 1)):
            yield cluster
            cluster, regional_count = "", 0
        cluster += char
        regional_count = regional_count + 1 if regional else 0
    if cluster:
        yield cluster


def _descendants(nodes):
    for node in nodes:
        yield node
        yield from _descendants(node.get("children", []))


def _tags(run):
    if run.code == "inline":
        return [("code", "")]
    if run.code == "block":
        return [("pre", ""), ("code", f' class="language-{run.language}"')] if run.language else [("pre", "")]
    tags = [("blockquote", "")] if run.quote else []
    if run.href:
        tags.append(("a", f' href="{escape(run.href, quote=True)}"'))
    tags.extend((mark, "") for mark in run.marks)
    return tags


def _payload(runs, formatted=True):
    html, plain, active = [], [], []
    link_text, link_url = "", None

    def finish_link():
        if link_url and link_text != link_url:
            plain.append(f" ({link_url})")

    for run in runs:
        if run.href != link_url:
            finish_link()
            link_text, link_url = "", run.href
        plain.append(run.text)
        link_text += run.text
        target = _tags(run)
        common = 0
        while common < min(len(active), len(target)) and active[common] == target[common]:
            common += 1
        html.extend(f"</{tag}>" for tag, _ in reversed(active[common:]))
        html.extend(f"<{tag}{attrs}>" for tag, attrs in target[common:])
        html.append(escape(run.text, quote=False))
        active = target
    finish_link()
    html.extend(f"</{tag}>" for tag, _ in reversed(active))
    fallback = "".join(plain)
    return {"text": "".join(html) if formatted else fallback, "parse_mode": "HTML" if formatted else None,
            "plain_text": fallback, "state": "pending"}


def _chunks(runs, formatted=True, budget=MAX_RENDERED_TEXT):
    result, current = [], []
    visible, plain, previous_href, entities = 0, 0, None, 0
    emitted = 0

    def append_chunk():
        nonlocal emitted
        payload = _payload(current, formatted)
        # Destinations repeat in both payloads, including after chunk splits and formatting changes.
        emitted += len(payload["text"]) + len(payload["plain_text"])
        if formatted and emitted > budget:
            raise ValueError("Markdown expansion limit")
        result.append(payload)

    for run in runs:
        remaining = run.text
        while remaining:
            if formatted and len(result) >= MAX_FORMATTED_CHUNKS:
                raise ValueError("Markdown delivery expansion limit")
            entity_cost = len(_tags(run)) if formatted else 0
            if current and entities + entity_cost > MAX_ENTITIES:
                append_chunk()
                current, visible, plain, previous_href, entities = [], 0, 0, None, 0
                continue
            extra = units(run.href) + 3 if run.href and previous_href != run.href else 0
            capacity = min(LIMIT - visible, LIMIT - plain - extra)
            if capacity <= 0:
                append_chunk()
                current, visible, plain, previous_href, entities = [], 0, 0, None, 0
                continue
            taken, count, newline_boundary, word_boundary = [], 0, 0, 0
            for cluster in clusters(remaining):
                size = units(cluster)
                if count + size > capacity:
                    break
                taken.append(cluster)
                count += size
                if cluster.endswith("\n"):
                    newline_boundary = len(taken)
                elif cluster.isspace():
                    word_boundary = len(taken)
            preferred = newline_boundary or word_boundary
            if not taken:
                if current:
                    append_chunk()
                    current, visible, plain, previous_href, entities = [], 0, 0, None, 0
                    continue
                # A pathological cluster may exceed one message; preserve its code points instead.
                for char in remaining:
                    if units(char) > capacity:
                        break
                    taken.append(char)
                    capacity -= units(char)
            elif sum(map(len, taken)) < len(remaining) and preferred:
                taken = taken[:preferred]
            part = "".join(taken)
            count = units(part)
            if not part:
                append_chunk()
                current, visible, plain, previous_href, entities = [], 0, 0, None, 0
                continue
            current.append(replace(run, text=part))
            visible += count
            plain += count + extra
            previous_href = run.href
            entities += entity_cost
            remaining = remaining[len(part):]
            if remaining:
                append_chunk()
                current, visible, plain, previous_href, entities = [], 0, 0, None, 0
    if current:
        append_chunk()
    return result


def format_reply(text):
    if not isinstance(text, str):
        raise ValueError("Reply must be text")
    if not text:
        return []
    if len(text) > MAX_MARKDOWN_SOURCE:
        return _chunks([Run(text)], formatted=False)
    try:
        parser = mistune.Markdown(inline=ChatInlineParser(), plugins=[
            mistune.plugins.import_plugin(name) for name in ("strikethrough", "table", "task_lists", "url")
        ])
        # Repeated table headers and tiny inline nodes must not amplify a bounded reply without limit.
        renderer = Renderer(min(MAX_RENDERED_TEXT, max(16384, len(text) * 8)))
        renderer.blocks(parser(text))
        while renderer.runs and renderer.runs[-1].separator:
            renderer.runs.pop()
        if not any(run.text.strip() for run in renderer.runs):
            return _chunks([Run(text)], formatted=False)
        return _chunks(renderer.runs, budget=renderer.budget)
    except (ValueError, KeyError, TypeError, RecursionError):
        return _chunks([Run(text)], formatted=False)
