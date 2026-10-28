"""Real HTTP parsing over deterministic synthetic socket buffers."""
import io
from http.client import HTTPResponse
from unittest.mock import Mock


class _SocketInput(io.RawIOBase):
    def __init__(self, wire, sock):
        self.source, self._sock = io.BytesIO(wire), sock

    def readable(self):
        return True

    def readinto(self, buffer):
        return self.source.readinto(buffer)

    def close(self):
        self.source.close()
        super().close()


class SyntheticSocket:
    def __init__(self, wire):
        self.wire, self.settimeout = wire, Mock()

    def makefile(self, mode):
        return io.BufferedReader(_SocketInput(self.wire, self))


class SyntheticResponse(HTTPResponse):
    def __init__(self, body, length=None):
        length = len(body) if length is None else length
        self.socket = SyntheticSocket(
            f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n".encode() + body
        )
        super().__init__(self.socket)
        self.begin()
        self.source = self.fp
        self.read_sizes, self.body_bytes = [], 0

    def read1(self, size):
        self.read_sizes.append(size)
        chunk = super().read1(size)
        self.body_bytes += len(chunk)
        return chunk
