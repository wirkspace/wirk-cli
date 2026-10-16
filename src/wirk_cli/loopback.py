"""This machine's end of a one-click login (wirk-core docs/plans/one-click-login.md §3): after the person approves,
their browser brings the one-time value to 127.0.0.1 here, and wirk login sends it back with the token."""

import queue
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

VALUE = re.compile(r"[A-Za-z0-9_-]{16,64}")  # the service's shape for the value; nothing else is passed on
PAGE = b"<!doctype html><meta charset=utf-8><title>WIRK</title><p>Connected. You can close this tab.\n"


class Done(BaseHTTPRequestHandler):
    """Only GET /done?c=VALUE addressed to 127.0.0.1:PORT; another path, a rebound name or a junk value gets 404."""

    def do_GET(self):
        parts = urlsplit(self.path)
        value = parse_qs(parts.query).get("c", [""])[0]
        if parts.path != "/done" or self.headers["Host"] != f"127.0.0.1:{self.server.port}" or not VALUE.fullmatch(value):
            return self.send_error(404)
        self.server.values.put(value)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):  # a request line carries the value: nothing is printed
        pass


class Listener(socketserver.TCPServer):
    """127.0.0.1 on a port the kernel chooses, answering in the background until closed."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Done)
        self.port, self.values = self.server_address[1], queue.Queue()
        threading.Thread(target=self.serve_forever, args=(0.1,), daemon=True).start()

    def get(self, seconds: float) -> str | None:
        """The next value a browser brought, waiting up to `seconds`."""
        try:
            return self.values.get(timeout=seconds)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.shutdown()
        self.server_close()
