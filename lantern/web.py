"""Private operator chat served on IPv4 loopback, behind operator SSH forwarding."""

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from .chat_store import ChatStore
from .chat_worker import worker_online


DEFAULT_PORT = 8765
PAGE_SIZE = 40
MAX_MESSAGE = 16000
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/room.css": ("room.css", "text/css; charset=utf-8"),
    "/room.js": ("room.js", "text/javascript; charset=utf-8"),
}


class RoomServer(ThreadingHTTPServer):
    def __init__(self, state: Path, port: int):
        self.state = state
        self.store = ChatStore(state)
        self.key = secrets.token_urlsafe(32)
        directory = files("lantern").joinpath("web_assets")
        self.assets = {path: (directory.joinpath(name).read_bytes(), mime)
                       for path, (name, mime) in ASSETS.items()}
        super().__init__(("127.0.0.1", port), RoomHandler)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def publish_key(self):
        # Publish only after a successful bind. An unsuccessful second launch
        # must not invalidate the link to the running service.
        stream = NamedTemporaryFile(dir=self.state, delete=False, mode="w", encoding="ascii")
        name = Path(stream.name)
        try:
            with stream:
                stream.write(self.key + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state / "web-key")
        finally:
            name.unlink(missing_ok=True)


class RoomHandler(BaseHTTPRequestHandler):
    server_version = "Lantern"
    sys_version = ""

    def log_message(self, format, *args):
        # Neither transcripts, credentials nor user-controlled request lines
        # belong in the web service's journal.
        pass

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "Unsupported request."})

    def _reply(self, status, data, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; "
                         "script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A tab can disconnect while an acknowledged write is in flight.

    def _json(self, status, value):
        self._reply(status, json.dumps(value, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def _check_host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.hosts:
            self._json(403, {"error": "Open the loopback address in the operator link."})
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [f"http://{hosts[0]}"]:
            self._json(403, {"error": "Request origin does not match the operator link."})
            return False
        return True

    def _authorized(self):
        auth = self.headers.get_all("Authorization", [])
        if len(auth) != 1 or not secrets.compare_digest(
                auth[0].encode("latin-1"), f"Bearer {self.server.key}".encode("ascii")):
            self._json(401, {"error": "Open a fresh operator link to reconnect."})
            return False
        return True

    def do_GET(self):
        if not self._check_host():
            return
        if self.path in self.server.assets:
            self._reply(200, *self.server.assets[self.path])
            return
        if self.path.partition("?")[0] != "/api/room":
            self._json(404, {"error": "Unknown route."})
            return
        if not self._authorized():
            return
        try:
            query = parse_qs(urlsplit(self.path).query, strict_parsing=True,
                             keep_blank_values=True, max_num_fields=1)
            direction, cursor = None, None
            if query:
                direction, values = next(iter(query.items()))
                if direction not in {"before", "after"} or not re.fullmatch(r"[0-9]{1,19}", values[0]):
                    raise ValueError
                cursor = int(values[0])
                if cursor > 2**63 - 1:
                    raise ValueError
        except ValueError:
            self._json(400, {"error": "Invalid history cursor."})
            return

        store = self.server.store
        try:
            if direction == "after":
                rows = store.events(after=cursor, limit=PAGE_SIZE + 1)
                events = rows[:PAGE_SIZE]
            else:
                rows = store.recent(limit=PAGE_SIZE + 1, before=cursor)
                events = rows[-PAGE_SIZE:]
            state = store.status()
            status = {"online": worker_online(self.server.state), "working": state["active_batch"] is not None,
                      "queued": state["queued"]}
        except (OSError, sqlite3.Error):
            self._json(503, {"error": "The conversation store is temporarily unavailable."})
            return
        self._json(200, {"events": [asdict(event) for event in events],
                         "more": len(rows) > PAGE_SIZE, "status": status})

    def do_POST(self):
        # Read a bounded, well-framed request before answering. Closing a
        # Windows socket with its request body unread can reset the connection
        # and discard the response (including a useful 401 after key rotation).
        lengths = self.headers.get_all("Content-Length", [])
        if "Transfer-Encoding" in self.headers or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
            self._json(400, {"error": "A valid content length is required."})
            return
        if int(lengths[0]) > 128000:
            self._json(413, {"error": "Message is too large."})
            return
        try:
            content = self.rfile.read(int(lengths[0]))
        except OSError:
            return
        if not self._check_host():
            return
        if self.path != "/api/messages":
            self._json(404, {"error": "Unknown route."})
            return
        if not self._authorized():
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Send JSON."})
            return
        try:
            if len(content) != int(lengths[0]):
                raise ValueError
            body = json.loads(content.decode("utf-8"))
            if not isinstance(body, dict) or set(body) != {"text", "client_id"}:
                raise ValueError
            message, client_id = body["text"], body["client_id"]
            if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
                raise ValueError
            message.encode("utf-8")  # Reject isolated JSON-escaped surrogates before storing.
            if not isinstance(client_id, str) or str(UUID(client_id)) != client_id:
                raise ValueError
        except (ValueError, OSError, RecursionError):
            self._json(400, {"error": f"Send text up to {MAX_MESSAGE} characters and a valid message ID."})
            return
        try:
            receipt = self.server.store.submit(message, client_id="web:" + client_id)
        except ValueError:
            self._json(409, {"error": "This message ID already belongs to different text."})
            return
        except sqlite3.Error:
            self._json(503, {"error": "The conversation store is temporarily unavailable."})
            return
        self._json(200, {"id": receipt})


def serve_web(state: Path, port: int = DEFAULT_PORT):
    with RoomServer(state, port) as server:
        server.publish_key()
        server.serve_forever()
