"""Bounded multipart parsing; authentication fields precede private file spools."""
import hashlib
import secrets
import tempfile

from python_multipart.multipart import MultipartParser, parse_options_header


MAX_BYTES = 20000000


def parse_upload(source, length, content_type, csrf, nonce_check):
    kind, options = parse_options_header(content_type)
    boundary = options.get(b"boundary", b"")
    if kind != b"multipart/form-data" or not 1 <= len(boundary) <= 200 or length > MAX_BYTES + 131072:
        raise ValueError("Invalid upload framing.")
    fields, files, part = {}, [], {}
    total, ended, parts = 0, False, 0

    def begin():
        nonlocal parts
        parts += 1
        if parts > 13:
            raise ValueError("Too many upload parts.")
        part.clear()
        part.update(
            headers={}, name=bytearray(), value=bytearray(), data=bytearray(), size=0, header_bytes=0, file=None
        )

    def header_field(data, start, end):
        part["name"].extend(data[start:end])
        part["header_bytes"] += end - start
        if part["header_bytes"] > 4096:
            raise ValueError("Upload headers too large.")

    def header_value(data, start, end):
        part["value"].extend(data[start:end])
        part["header_bytes"] += end - start
        if part["header_bytes"] > 4096:
            raise ValueError("Upload headers too large.")

    def header_end():
        key = bytes(part["name"]).lower()
        if key in part["headers"]:
            raise ValueError("Duplicate upload header.")
        part["headers"][key] = bytes(part["value"])
        part["name"].clear()
        part["value"].clear()

    def headers_finished():
        kind, options = parse_options_header(part["headers"].get(b"content-disposition", b""))
        if kind != b"form-data" or b"name" not in options or b"content-transfer-encoding" in part["headers"]:
            raise ValueError("Invalid upload part.")
        part["field"] = options[b"name"].decode("utf-8")
        if b"filename" in options:
            if part["field"] != "files" or "csrf" not in fields or "nonce" not in fields:
                raise ValueError("Authentication fields must precede files.")
            if len(files) >= 10:
                raise ValueError("Maximum ten files.")
            filename = options[b"filename"].decode("utf-8")
            if not filename:
                part["skip"] = True
                return
            if len(filename) > 200 or any(ord(c) < 32 or c in "/\\" for c in filename):
                raise ValueError("Invalid filename.")
            mime = (
                part["headers"]
                .get(b"content-type", b"application/octet-stream")
                .decode("ascii")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if mime.startswith("multipart/"):
                raise ValueError("Nested multipart uploads are unsupported.")
            stream = tempfile.TemporaryFile("w+b")
            item = {
                "filename": filename,
                "mime": mime,
                "size": 0,
                "sha256": None,
                "stream": stream,
                "digest": hashlib.sha256(),
            }
            files.append(item)
            part["file"] = item
        elif part["field"] not in ("csrf", "nonce", "text") or part["field"] in fields:
            raise ValueError("Unexpected or duplicate form field.")

    def data(data, start, end):
        nonlocal total
        value = data[start:end]
        if part.get("skip"):
            if value:
                raise ValueError("Unnamed file.")
            return
        if part["file"] is not None:
            item = part["file"]
            total += len(value)
            item["size"] += len(value)
            if total > MAX_BYTES:
                raise ValueError("Files exceed 20 MB combined.")
            item["stream"].write(value)
            item["digest"].update(value)
        else:
            part["data"].extend(value)
            if len(part["data"]) > 64000:
                raise ValueError("Form field too large.")

    def finish_part():
        if part.get("skip"):
            return
        if part["file"] is not None:
            item = part["file"]
            if not item["size"]:
                raise ValueError("Empty files are unsupported.")
            item["sha256"] = item.pop("digest").hexdigest()
            item["stream"].seek(0)
        else:
            value = part["data"].decode("utf-8")
            key = part["field"]
            if key == "csrf" and not secrets.compare_digest(value, csrf):
                raise PermissionError("Invalid upload authentication.")
            if key == "nonce":
                if "csrf" not in fields:
                    raise ValueError("Authentication fields out of order.")
                nonce_check(value)
            fields[key] = value

    def finish():
        nonlocal ended
        ended = True

    callbacks = {
        "on_part_begin": begin,
        "on_header_field": header_field,
        "on_header_value": header_value,
        "on_header_end": header_end,
        "on_headers_finished": headers_finished,
        "on_part_data": data,
        "on_part_end": finish_part,
        "on_end": finish,
    }
    parser = MultipartParser(boundary, callbacks, max_size=MAX_BYTES + 131072)
    try:
        remaining = length
        while remaining:
            block = source.read(min(65536, remaining))
            if not block:
                raise ValueError("Incomplete upload.")
            parser.write(block)
            remaining -= len(block)
        parser.finalize()
        if not ended or set(fields) != {"csrf", "nonce", "text"}:
            raise ValueError("Incomplete upload form.")
        if len(fields["text"]) > 16000 or not fields["text"].strip() and not files:
            raise ValueError("Supply a message or files.")
        return fields, files
    except BaseException:
        for item in files:
            item["stream"].close()
        raise
