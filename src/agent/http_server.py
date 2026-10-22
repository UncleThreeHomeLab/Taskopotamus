import threading
from http.server import ThreadingHTTPServer


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, handler, max_connections=8):
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        # Bound threads before spawning them, including clients that never send headers.
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(10)
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Requests and SDK exceptions can contain sensitive data.
        pass
