"""Private operator chat served on loopback for SSH forwarding or an HTTPS proxy."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from . import operator_release
from .attachments import MAX_IMAGE_BASE64, MAX_TURN_IMAGES, parse_image
from .chat_store import ChatStore
from .chat_worker import worker_online
from .compaction_annotations import annotation_text
from .file_grants import FileGrants
from .clone_grants import CloneGrants
from .operator_review import MAX_ANSWER, ReviewRoom
from .web_auth import LoginThrottled, SESSION_SECONDS, WebAuth


DEFAULT_PORT = 8765
PAGE_SIZE = 40
MAX_MESSAGE = 16000
TEXT_BODY_LIMIT = 128000
MESSAGE_BODY_LIMIT = TEXT_BODY_LIMIT + MAX_IMAGE_BASE64 * MAX_TURN_IMAGES
SIGN_IN_BODY_LIMIT = 4096
SESSION_COOKIE = "__Host-lantern-session"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/room.css": ("room.css", "text/css; charset=utf-8"),
    "/room.js": ("room.js", "text/javascript; charset=utf-8"),
    "/markdown-it-15.0.3.min.js": ("markdown-it-15.0.3.min.js", "text/javascript; charset=utf-8"),
    "/message-format.js": ("message-format.js", "text/javascript; charset=utf-8"),
    "/transcript-rtf.js": ("transcript-rtf.js", "text/javascript; charset=utf-8"),
}


def _event_json(event):
    return {**asdict(event), "image": event.image}


def _annotations(store, first_anchor, last_anchor, *, since=None):
    """Return separate timeline rows. Anchor zero places a checkpoint before the first room event."""
    if first_anchor > last_anchor:
        return []
    with store._connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='compaction_annotations'").fetchone():
            return []
        rows = db.execute("""SELECT c.id, c.after_event_id, c.occurred_at, c.recorded_at
            FROM compaction_annotations c JOIN room r ON r.id=1 AND r.thread_id=c.thread_id
            WHERE COALESCE(c.after_event_id,0) BETWEEN ? AND ?
            ORDER BY c.after_event_id, c.occurred_at, c.id""", (first_anchor, last_anchor))
        return [{"ref": f"annotation:{row['id']}", "role": "annotation", "after_event_id": row["after_event_id"],
                 "at": row["occurred_at"], "recorded_at": row["recorded_at"], "text": annotation_text(row["occurred_at"])}
                for row in rows if since is None or datetime.fromisoformat(row["occurred_at"]) >= since]


def canonical_origin(origin: str) -> str:
    """Normalize a configured HTTPS origin to the Host and Origin browsers send."""
    if not re.fullmatch(r"https://(?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\])(?::[0-9]+)?/?", origin):
        raise ValueError("Supply an HTTPS origin with a hostname and optional port")
    address = urlsplit(origin)
    host = address.hostname
    port = address.port
    if not host or port == 0:
        raise ValueError("Invalid HTTPS origin")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


class RoomServer(ThreadingHTTPServer):
    def __init__(self, state: Path, port: int, *, public_origin: str | None = None):
        self.public_origin = canonical_origin(public_origin) if public_origin is not None else None
        self.state = state
        self.store = ChatStore(state)
        self.file_grants = FileGrants(state)
        self.clone_grants = CloneGrants(state)
        self.reviews = ReviewRoom(self.store)
        # Large JSON uploads need simultaneous buffers for UTF-8, base64, the
        # verified raster and the SQLite write. Keep room inside MemoryMax.
        self.large_uploads = BoundedSemaphore(2)
        self.key = None if self.public_origin else secrets.token_urlsafe(32)
        self.auth = WebAuth(state) if self.public_origin else None
        directory = files("lantern").joinpath("web_assets")
        self.assets = {path: (directory.joinpath(name).read_bytes(), mime)
                       for path, (name, mime) in ASSETS.items()}
        if self.public_origin:
            self.login_page = directory.joinpath("login.html").read_bytes()
            self.assets["/login.js"] = (directory.joinpath("login.js").read_bytes(), "text/javascript; charset=utf-8")
        super().__init__(("127.0.0.1", port), RoomHandler)
        self.hosts = ({urlsplit(self.public_origin).netloc} if self.public_origin else
                      {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"})

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def publish_key(self):
        if self.key is None:
            return
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

    def _reply(self, status, data, mime, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; "
                         "script-src 'self'; style-src 'self'; connect-src 'self'; img-src data: blob:; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if self.server.public_origin:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A tab can disconnect while an acknowledged write is in flight.

    def _json(self, status, value, headers=()):
        self._reply(status, json.dumps(value, separators=(",", ":")).encode(), "application/json; charset=utf-8", headers)

    def _check_host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.hosts:
            self._json(403, {"error": "Open the configured operator address."})
            return False
        origins = self.headers.get_all("Origin", [])
        expected = self.server.public_origin or f"http://{hosts[0]}"
        origin_required = self.server.public_origin is not None and self.command == "POST"
        if (origins or origin_required) and origins != [expected]:
            self._json(403, {"error": "This request must come from the operator room."})
            return False
        return True

    def _session_token(self):
        values = []
        for header in self.headers.get_all("Cookie", []):
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == SESSION_COOKIE:
                    values.append(value)
        return values[0] if len(values) == 1 else ""

    def _signed_in(self):
        """None means the store failed and the HTTP error was already sent."""
        token = self._session_token()
        try:
            return bool(token and self.server.auth.valid_session(token))
        except (sqlite3.Error, OSError):
            self._json(503, {"error": "The sign-in service is temporarily unavailable."})
            return None

    def _authorized(self):
        if self.server.auth is not None:
            valid = self._signed_in()
            if valid is False:
                self._json(401, {"error": "Sign in to open the operator room."})
            return valid is True
        auth = self.headers.get_all("Authorization", [])
        if len(auth) != 1 or not secrets.compare_digest(
                auth[0].encode("latin-1"), f"Bearer {self.server.key}".encode("ascii")):
            self._json(401, {"error": "Open a fresh operator link to reconnect."})
            return False
        return True

    def do_GET(self):
        if not self._check_host():
            return
        if self.path == "/" and self.server.auth is not None:
            signed_in = self._signed_in()
            if signed_in is None:
                return
            if not signed_in:
                self._reply(200, self.server.login_page, "text/html; charset=utf-8")
                return
        if self.path in self.server.assets:
            self._reply(200, *self.server.assets[self.path])
            return
        image_route = re.fullmatch(r"/api/messages/([1-9][0-9]{0,18})/(?:image|images/(0|[1-9][0-9]?))", self.path)
        if image_route:
            if not self._authorized():
                return
            event_id = int(image_route[1])
            try:
                index = int(image_route[2] or 0)
                data = self.server.store.image_data(event_id, index) if event_id < 2**63 else None
            except sqlite3.Error:
                self._json(503, {"error": "The conversation store is temporarily unavailable."})
                return
            if data is None:
                self._json(404, {"error": "No image on this message."})
            else:
                self._reply(200, data, "image/png")
            return
        if self.path == "/api/review-questions":
            if self._authorized():
                try:
                    self._json(200, {"questions": self.server.reviews.pending()})
                except sqlite3.Error:
                    self._json(503, {"error": "The review store is temporarily unavailable."})
            return
        if self.path == "/api/concerns":
            if self._authorized():
                try:
                    self._json(200, operator_release.sidebar(self.server.store))
                except sqlite3.Error:
                    self._json(503, {"error": "The concern list is temporarily unavailable."})
            return
        if self.path in ("/api/file-imports", "/api/repository-clones"):
            if self._authorized():
                ledger = self.server.file_grants if self.path == "/api/file-imports" else self.server.clone_grants
                try:
                    self._json(200, ledger.overview())
                except (sqlite3.Error, OSError):
                    self._json(503, {"error": "Room permissions are temporarily unavailable."})
            return
        path = self.path.partition("?")[0]
        if path == "/api/export":
            if self._authorized():
                self._export_page()
            return
        if path != "/api/room":
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
                first_anchor = cursor + 1 if cursor else 0
            else:
                rows = store.recent(limit=PAGE_SIZE + 1, before=cursor)
                events = rows[-PAGE_SIZE:]
                first_anchor = events[0].id if len(rows) > PAGE_SIZE else 0
            annotations = _annotations(store, first_anchor, events[-1].id if events else 0)
            state = store.status()
            online = worker_online(self.server.state)
            status = {"online": online, "working": state["active_batch"] is not None,
                      "compacting": online and state["compacting"],
                      "queued": state["queued"]}
        except (OSError, sqlite3.Error):
            self._json(503, {"error": "The conversation store is temporarily unavailable."})
            return
        self._json(200, {"events": [_event_json(event) for event in events],
                         "annotations": annotations,
                         "more": len(rows) > PAGE_SIZE, "status": status})

    def _export_page(self):
        store = self.server.store
        after, through, since = 0, None, None
        try:
            query = parse_qs(urlsplit(self.path).query, strict_parsing=True,
                             keep_blank_values=True, max_num_fields=1)
            if "days" in query:
                value = query["days"][0]
                if not re.fullmatch(r"[1-9][0-9]{0,5}", value) or int(value) > 365000:
                    raise ValueError
                since = store.clock().astimezone(timezone.utc) - timedelta(days=int(value))
            elif "cursor" in query:
                match = re.fullmatch(r"([1-9][0-9]{0,18})~([1-9][0-9]{0,18})~([0-9]{20})?", query["cursor"][0])
                if match is None:
                    raise ValueError
                after, through = int(match[1]), int(match[2])
                if not after < through < 2**63:
                    raise ValueError
                if match[3]:
                    since = datetime.strptime(match[3], "%Y%m%d%H%M%S%f").replace(tzinfo=timezone.utc)
            elif query:
                raise ValueError
        except (ValueError, OverflowError):
            self._json(400, {"error": "Invalid export range or cursor."})
            return
        try:
            through, rows = store.export_page(after=after, through=through, since=since, limit=PAGE_SIZE + 1)
            events = rows[:PAGE_SIZE]
            last_anchor = events[-1].id if len(rows) > PAGE_SIZE else through
            annotations = _annotations(store, after + 1 if after else 0, last_anchor, since=since)
        except sqlite3.Error:
            self._json(503, {"error": "The conversation store is temporarily unavailable."})
            return
        cursor = None
        if len(rows) > PAGE_SIZE:
            cutoff = since.strftime("%Y%m%d%H%M%S%f") if since else ""
            cursor = f"{events[-1].id}~{through}~{cutoff}"
        self._json(200, {"events": [_event_json(event) for event in events],
                         "annotations": annotations,
                         "next_cursor": cursor})

    def do_POST(self):
        # Read a bounded, well-framed request before answering. Closing a
        # Windows socket with its request body unread can reset the connection
        # and discard the response (including a useful 401 after key rotation).
        lengths = self.headers.get_all("Content-Length", [])
        if "Transfer-Encoding" in self.headers or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
            self._json(400, {"error": "A valid content length is required."})
            return
        length = int(lengths[0])
        max_body = MESSAGE_BODY_LIMIT if self.path == "/api/messages" else TEXT_BODY_LIMIT
        if self.path in ("/api/sign-in", "/api/sign-out"):
            max_body = SIGN_IN_BODY_LIMIT
        if length > max_body:
            self._json(413, {"error": "Message is too large."})
            self._drain_rejected(length)
            return
        if length <= TEXT_BODY_LIMIT:
            self._post_body(length)
            return
        # A large upload has a private route. Authenticate before reserving
        # memory or reading its payload; small chat/control requests can proceed.
        if not self._check_host() or not self._authorized():
            self._drain_rejected(length)
            return
        if not self.server.large_uploads.acquire(blocking=False):
            self._json(503, {"error": "Image uploads are busy. Retry your message shortly."})
            self._drain_rejected(length)
            return
        try:
            self._post_body(length)
        finally:
            self.server.large_uploads.release()

    def _drain_rejected(self, length):
        # Windows may reset sockets closed with unread data. Drain briefly
        # after sending an error so an ordinary client can receive it.
        self.close_connection = True
        remaining = min(length, MESSAGE_BODY_LIMIT)
        deadline = monotonic() + 0.25
        try:
            while remaining and (timeout := deadline - monotonic()) > 0:
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(min(remaining, 32 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _post_body(self, length):
        try:
            content = self.rfile.read(length)
        except OSError:
            return
        if not self._check_host():
            return
        if self.server.auth is not None and self.path in ("/api/sign-in", "/api/sign-out"):
            self._change_session(content, length)
            return
        answer_route = re.fullmatch(r"/api/review-questions/([1-9][0-9]{0,18})/answer", self.path)
        release_route = re.fullmatch(r"/api/concerns/([1-9][0-9]{0,18})/release", self.path)
        import_route = re.fullmatch(r"/api/file-imports(?:/([1-9][0-9]{0,18})/(decide|revoke))?", self.path)
        clone_route = re.fullmatch(r"/api/repository-clones/([1-9][0-9]{0,18})/(decide|revoke|reopen)", self.path)
        if self.path != "/api/messages" and not any((answer_route, release_route, import_route, clone_route)):
            self._json(404, {"error": "Unknown route."})
            return
        if not self._authorized():
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Send JSON."})
            return
        try:
            if len(content) != length:
                raise ValueError
            body = json.loads(content.decode("utf-8"))
            if import_route:
                self._change_grant(self.server.file_grants, import_route, body)
                return
            if clone_route:
                self._change_grant(self.server.clone_grants, clone_route, body)
                return
            if release_route:
                number = int(release_route[1])
                if (number >= 2**63 or not isinstance(body, dict)
                        or set(body) not in ({"version"}, {"version", "reason"})):
                    raise ValueError
                try:
                    event_id = operator_release.queue_release(
                        self.server.store, number, body["version"], body.get("reason", ""))
                except KeyError:
                    self._json(404, {"error": "This concern is no longer in the standing concerns list."})
                except operator_release.ReleaseConflict as exc:
                    self._json(409, {"error": str(exc)})
                except sqlite3.Error:
                    self._json(503, {"error": "The release could not be queued."})
                else:
                    self._json(202, {"id": event_id})
                return
            if answer_route:
                review_id = int(answer_route[1])
                if (review_id > 2**63 - 1 or not isinstance(body, dict) or set(body) != {"answer"}
                        or not isinstance(body["answer"], str) or not body["answer"].strip()
                        or len(body["answer"]) > MAX_ANSWER):
                    raise ValueError
                body["answer"].encode("utf-8")
                try:
                    self.server.reviews.answer(review_id, body["answer"])
                except KeyError:
                    self._json(404, {"error": "Unknown review question."})
                except ValueError:
                    self._json(409, {"error": "A different answer was already recorded."})
                except sqlite3.Error:
                    self._json(503, {"error": "The review store is temporarily unavailable."})
                else:
                    self._json(200, {"id": review_id})
                return
            if (not isinstance(body, dict) or set(body) not in
                    ({"text", "client_id"}, {"text", "client_id", "image"}, {"text", "client_id", "images"})):
                raise ValueError
            message, client_id = body["text"], body["client_id"]
            uploads = [body["image"]] if "image" in body else body.get("images", [])
            if not isinstance(uploads, list) or len(uploads) > MAX_TURN_IMAGES:
                self._json(400, {"error": f"Attach up to {MAX_TURN_IMAGES} images per message."})
                return
            if not isinstance(message, str) or len(message) > MAX_MESSAGE or (not message.strip() and not uploads):
                raise ValueError
            message.encode("utf-8")  # Reject isolated JSON-escaped surrogates before storing.
            if not isinstance(client_id, str) or str(UUID(client_id)) != client_id:
                raise ValueError
        except (ValueError, OSError, RecursionError):
            error = ("Supply a valid repository grant decision." if clone_route else
                     "Supply a valid file import request." if import_route else
                     "Supply the displayed concern version and an optional single-line reason up to 600 characters." if release_route else
                     f"Send one answer of 1 to {MAX_ANSWER} characters." if answer_route else
                     f"Send text up to {MAX_MESSAGE} characters and a valid message ID.")
            self._json(400, {"error": error})
            return
        try:
            images = [parse_image(upload) for upload in uploads]
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            receipt = self.server.store.submit(message, client_id="web:" + client_id, images=images)
        except ValueError:
            self._json(409, {"error": "This message ID already belongs to different text or attachments."})
            return
        except sqlite3.Error:
            self._json(503, {"error": "The conversation store is temporarily unavailable."})
            return
        self._json(200, {"id": receipt})

    def _change_grant(self, grants, route, body):
        if not isinstance(body, dict):
            raise ValueError
        number = int(route[1]) if route[1] else None
        if number is not None and number >= 2**63:
            raise ValueError
        try:
            if route[2] == "decide":
                if set(body) != {"decision"} or body["decision"] not in ("allow_once", "allow_standing", "deny"):
                    raise ValueError("Choose allow once, standing permission or deny.")
                grants.decide(number, body["decision"])
            elif route[2] == "revoke":
                if body:
                    raise ValueError("Revoking a grant takes no additional fields.")
                grants.revoke(number)
            elif route[2] == "reopen":
                if body:
                    raise ValueError("Reconsidering this request takes no additional fields.")
                grants.reopen(number)
            else:
                if set(body) != {"project", "collection", "files"}:
                    raise ValueError("Supply a project, collection and list of files.")
                grants.propose(body["project"], body["collection"], body["files"], operator=True)
        except KeyError:
            self._json(404, {"error": "Unknown request or grant."})
        except ValueError as exc:
            self._json(400, {"error": str(exc) or "Invalid request or grant."})
        except (sqlite3.Error, OSError):
            self._json(503, {"error": "Room permissions are temporarily unavailable."})
        else:
            self._json(200, {"ok": True})

    def _change_session(self, content, length):
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Send JSON."})
            return
        try:
            if len(content) != length:
                raise ValueError
            body = json.loads(content.decode("utf-8"))
            if self.path == "/api/sign-in":
                if not isinstance(body, dict) or set(body) != {"password"} or not isinstance(body["password"], str):
                    raise ValueError
                token = self.server.auth.sign_in(body["password"])
                if token is None:
                    self._json(401, {"error": "Sign-in failed. Check your password."})
                    return
                lifetime = SESSION_SECONDS
            else:
                if body != {}:
                    raise ValueError
                token = self._session_token()
                if token:
                    self.server.auth.sign_out(token)
                token, lifetime = "", 0
        except LoginThrottled as exc:
            self._json(429, {"error": "Please wait before trying again."}, [("Retry-After", str(exc.retry_after))])
            return
        except (ValueError, RecursionError):
            self._json(400, {"error": "Invalid sign-in request."})
            return
        except (OSError, sqlite3.Error):
            self._json(503, {"error": "The sign-in service is temporarily unavailable."})
            return
        cookie = f"{SESSION_COOKIE}={token}; Path=/; Max-Age={lifetime}; Secure; HttpOnly; SameSite=Strict"
        self._json(200, {"ok": True}, [("Set-Cookie", cookie)])


def serve_web(state: Path, port: int = DEFAULT_PORT, *, public_origin: str | None = None):
    with RoomServer(state, port, public_origin=public_origin) as server:
        server.publish_key()
        server.serve_forever()
