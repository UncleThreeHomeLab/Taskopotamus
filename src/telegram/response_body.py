"""Bound Telegram response bodies without resetting the transfer deadline."""
import io
import time
from http.client import IncompleteRead


class DeadlineExpired(TimeoutError):
    pass


def remaining(deadline, timeout=10):
    duration = deadline - time.monotonic()
    if duration <= 0:
        raise DeadlineExpired("Telegram transfer deadline expired.")
    return min(timeout, duration)


class _DeadlineReader(io.RawIOBase):
    def __init__(self, source, deadline, timeout):
        self.source, self.deadline, self.timeout = source, deadline, timeout

    def readable(self):
        return True

    def readinto(self, buffer):
        self.source.raw._sock.settimeout(remaining(self.deadline, self.timeout))
        chunk = self.source.read1(len(buffer))
        remaining(self.deadline, self.timeout)
        buffer[:len(chunk)] = chunk
        return len(chunk)

    def close(self):
        if not self.closed:
            try:
                self.source.close()
            finally:
                super().close()


def read_chunk(response, size, deadline, timeout=10):
    remaining(deadline, timeout)
    if getattr(response, "fp", None) is None:
        chunk = b""
    else:
        # Guard underlying reads too: HTTP chunk framing can read repeatedly before yielding payload.
        if not isinstance(response.fp.raw, _DeadlineReader):
            response.fp = io.BufferedReader(_DeadlineReader(response.fp, deadline, timeout))
        chunk = response.read1(size)
    remaining(deadline, timeout)
    if not chunk and getattr(response, "length", None):
        raise IncompleteRead(b"", response.length)
    return chunk


def read_body(response, limit, deadline, timeout=10):
    body = bytearray()
    while len(body) <= limit:
        chunk = read_chunk(response, min(65536, limit + 1 - len(body)), deadline, timeout)
        if not chunk:
            break
        body.extend(chunk)
    return body
