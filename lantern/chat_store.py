"""Persistent single-operator chat, ordered delivery and crash recovery."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from .attachments import ImageAttachment, MAX_TURN_IMAGES
from . import operator_release
from .chat_text import trim_reply_segment
from .local_time import central_time
from .routing import ROUTES, validate_route
from .scheduler import SCHEMA, next_reset


@dataclass(frozen=True)
class Event:
    id: int
    role: str
    text: str
    at: str
    images: tuple[dict, ...] = ()

    @property
    def image(self):
        return self.images[0] if self.images else None


# The room and CLI read metadata here. Image bytes are loaded only for an
# authenticated preview or a model request that needs its recent images.
EVENT_SELECT = """SELECT e.id,e.role,e.text,e.at,im.width,im.height,im.sha256,length(im.data) AS size
    FROM events e LEFT JOIN message_images im ON im.event_id=e.id """

# Each private page raster is at most 1 MiB. Keep recent views across restarts
# without letting an autonomous reader fill the host volume with derived pages.
MAX_CACHED_PDF_RASTERS = 64


def _image_metadata(row):
    return {"mime_type": "image/png", "width": row["width"], "height": row["height"],
            "sha256": row["sha256"], "size": row["size"]}


def _event(db, row):
    images = []
    if row["sha256"] is not None:
        images.append(_image_metadata(row))
        extra = db.execute("""SELECT width,height,sha256,length(data) AS size
            FROM extra_message_images WHERE event_id=? ORDER BY position""", (row["id"],))
        images.extend(_image_metadata(item) for item in extra)
    return Event(row["id"], row["role"], row["text"], row["at"], tuple(images))


@dataclass(frozen=True)
class Batch:
    id: int
    thread_id: str | None
    messages: tuple[Event, ...]


class ChatStore:
    def __init__(self, state: Path, *, clock=None):
        state.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = state / "chat.sqlite"
        self._next_concern_snapshot = 0
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS room (
                    id INTEGER PRIMARY KEY CHECK (id = 1), thread_id TEXT, read_cursor INTEGER);
                INSERT OR IGNORE INTO room(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS provider_route_changes (
                    id INTEGER PRIMARY KEY, call_id TEXT NOT NULL UNIQUE, route TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL,
                    at TEXT NOT NULL, client_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS message_images (
                    event_id INTEGER PRIMARY KEY REFERENCES events(id), width INTEGER NOT NULL,
                    height INTEGER NOT NULL, sha256 TEXT NOT NULL, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS extra_message_images (
                    event_id INTEGER NOT NULL REFERENCES message_images(event_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL CHECK(position > 0), width INTEGER NOT NULL,
                    height INTEGER NOT NULL, sha256 TEXT NOT NULL, data BLOB NOT NULL,
                    PRIMARY KEY(event_id,position));
                CREATE TABLE IF NOT EXISTS pdf_rasters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS pdf_views (
                    thread_id TEXT NOT NULL, call_id TEXT NOT NULL,
                    raster_id INTEGER NOT NULL REFERENCES pdf_rasters(id) ON DELETE CASCADE,
                    PRIMARY KEY(thread_id, call_id));
                CREATE INDEX IF NOT EXISTS pdf_views_by_raster ON pdf_views(raster_id);
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                    outcome INTEGER REFERENCES events(id));
                CREATE UNIQUE INDEX IF NOT EXISTS single_active_batch
                    ON batches(status) WHERE status = 'active';
                CREATE TABLE IF NOT EXISTS inbox (
                    event_id INTEGER PRIMARY KEY REFERENCES events(id),
                    batch_id INTEGER REFERENCES batches(id), steered INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS inbox_by_batch ON inbox(batch_id, event_id);
                CREATE TABLE IF NOT EXISTS notifications (
                    batch_id INTEGER NOT NULL REFERENCES batches(id), call_id TEXT NOT NULL,
                    event_id INTEGER UNIQUE NOT NULL REFERENCES events(id),
                    PRIMARY KEY(batch_id, call_id));
            """ + SCHEMA + operator_release.SCHEMA)

    @contextmanager
    def _connect(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def _append(self, db, role, text, client_id=None):
        now = self.clock().isoformat()
        return db.execute("INSERT INTO events(role,text,at,client_id) VALUES(?,?,?,?)",
                          (role, text, now, client_id)).lastrowid

    def submit(self, text: str, client_id: str | None = None, *, image: ImageAttachment | None = None,
               images: tuple[ImageAttachment, ...] | list[ImageAttachment] = ()) -> int:
        """Accept a message durably. Reusing its client ID returns its original receipt."""
        if not isinstance(images, (list, tuple)) or len(images) > MAX_TURN_IMAGES or (image is not None and images):
            raise ValueError(f"Send up to {MAX_TURN_IMAGES} ordered message images")
        images = (image,) if image is not None else tuple(images)
        if not all(isinstance(item, ImageAttachment) for item in images):
            raise ValueError("Invalid message image")
        if not isinstance(text, str) or (not text.strip() and not images):
            raise ValueError("The chat message is empty")
        if client_id is not None and (not isinstance(client_id, str) or not client_id.strip()):
            raise ValueError("A supplied message ID must be a nonempty string")
        with self._connect(write=True) as db:
            if client_id is not None:
                existing = db.execute(EVENT_SELECT + "WHERE e.client_id=?", (client_id,)).fetchone()
                if existing is not None:
                    prior_images = _event(db, existing).images
                    if existing["text"] != text or [i["sha256"] for i in prior_images] != [i.sha256 for i in images]:
                        raise ValueError("This message ID already belongs to different text or image")
                    return existing["id"]
            event_id = self._append(db, "operator", text, client_id)
            if images:
                # Position zero stays in its original table for existing databases and older readers.
                image = images[0]
                db.execute("INSERT INTO message_images(event_id,width,height,sha256,data) VALUES(?,?,?,?,?)",
                           (event_id, image.width, image.height, image.sha256, image.data))
                db.executemany("""INSERT INTO extra_message_images(event_id,position,width,height,sha256,data)
                    VALUES(?,?,?,?,?,?)""", ((event_id, pos, item.width, item.height, item.sha256, item.data)
                                            for pos, item in enumerate(images[1:], start=1)))
            db.execute("INSERT INTO inbox(event_id) VALUES(?)", (event_id,))
            return event_id

    def image_data(self, event_id: int, index: int = 0) -> bytes | None:
        if type(index) is not int or not 0 <= index < MAX_TURN_IMAGES:
            return None
        with self._connect() as db:
            if index == 0:
                row = db.execute("SELECT data FROM message_images WHERE event_id=?", (event_id,)).fetchone()
            else:
                row = db.execute("SELECT data FROM extra_message_images WHERE event_id=? AND position=?",
                                 (event_id, index)).fetchone()
            return row["data"] if row else None

    def image_data_by_digest(self, event_id: int, sha256: str) -> bytes | None:
        """Resolve a saved model reference within its original operator message."""
        with self._connect() as db:
            row = db.execute("""SELECT data FROM message_images WHERE event_id=? AND sha256=?
                UNION ALL SELECT data FROM extra_message_images WHERE event_id=? AND sha256=?
                LIMIT 1""", (event_id, sha256, event_id, sha256)).fetchone()
            return row["data"] if row else None

    def save_page_image(self, thread_id: str, call_id: str, image: ImageAttachment) -> dict:
        """Keep the reviewed snapshot even if its source PDF is later edited or removed."""
        with self._connect(write=True) as db:
            db.execute("INSERT INTO pdf_rasters(sha256,data) VALUES(?,?) ON CONFLICT(sha256) DO NOTHING",
                       (image.sha256, image.data))
            raster_id = db.execute("SELECT id FROM pdf_rasters WHERE sha256=?", (image.sha256,)).fetchone()[0]
            db.execute("INSERT INTO pdf_views VALUES(?,?,?) ON CONFLICT(thread_id,call_id) DO NOTHING",
                       (thread_id, call_id, raster_id))
            prior = db.execute("SELECT raster_id FROM pdf_views WHERE thread_id=? AND call_id=?",
                               (thread_id, call_id)).fetchone()[0]
            if prior != raster_id:
                raise ValueError("This call already returned a different PDF page image.")
            db.execute("""DELETE FROM pdf_rasters WHERE id IN (
                SELECT raster_id FROM pdf_views GROUP BY raster_id ORDER BY MAX(rowid) DESC LIMIT -1 OFFSET ?)""",
                       (MAX_CACHED_PDF_RASTERS,))
        return {"id": raster_id, "sha256": image.sha256}

    def page_image_data(self, raster_id: int, call_id: str, *, thread_id: str) -> bytes | None:
        with self._connect() as db:
            row = db.execute("""SELECT r.data FROM pdf_views v JOIN pdf_rasters r ON r.id=v.raster_id
                                WHERE v.thread_id=? AND v.call_id=? AND r.id=?""",
                             (thread_id, call_id, raster_id)).fetchone()
            return row["data"] if row else None

    def events(self, after: int = 0, limit: int = 100) -> list[Event]:
        if after < 0 or limit < 0:
            raise ValueError("Invalid chat event cursor or limit")
        with self._connect() as db:
            return [_event(db, row) for row in db.execute(
                EVENT_SELECT + "WHERE e.id>? ORDER BY e.id LIMIT ?", (after, limit))]

    def recent(self, limit: int = 20, *, before: int | None = None) -> list[Event]:
        if limit < 0 or (before is not None and before < 0):
            raise ValueError("Invalid chat history cursor or limit")
        query = EVENT_SELECT
        params = []
        if before is not None:
            query += "WHERE e.id < ?"
            params.append(before)
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY e.id DESC LIMIT ?", (*params, limit)).fetchall()
            return [_event(db, row) for row in reversed(rows)]

    def read_cursor(self) -> int | None:
        with self._connect() as db:
            return db.execute("SELECT read_cursor FROM room WHERE id=1").fetchone()[0]

    def mark_read(self, event_id: int) -> None:
        with self._connect(write=True) as db:
            if event_id < 0 or (event_id and not db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone()):
                raise ValueError("Unknown chat event")
            db.execute("UPDATE room SET read_cursor=MAX(COALESCE(read_cursor,0),?) WHERE id=1", (event_id,))

    def status(self) -> dict:
        with self._connect() as db:
            row = db.execute("""SELECT thread_id,
                (SELECT id FROM batches WHERE status='active') AS active_batch,
                (SELECT COUNT(*) FROM inbox WHERE batch_id IS NULL) AS queued
                FROM room WHERE id=1""").fetchone()
            return dict(row)

    def provider_route(self, default: str) -> str:
        """Use the worker startup preference until Lantern makes its first choice."""
        validate_route(default)
        with self._connect() as db:
            row = db.execute("SELECT route FROM provider_route_changes ORDER BY id DESC LIMIT 1").fetchone()
            return row["route"] if row else default

    def set_provider_route(self, route: str, *, call_id: str) -> dict:
        """An approved call records its choice once. Replays never undo later changes."""
        validate_route(route)
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("A routing change needs a nonempty tool call ID.")
        with self._connect(write=True) as db:
            prior = db.execute("SELECT route FROM provider_route_changes WHERE call_id=?", (call_id,)).fetchone()
            if prior is None:
                db.execute("INSERT INTO provider_route_changes(call_id,route) VALUES(?,?)", (call_id, route))
            elif prior["route"] != route:
                raise ValueError("This tool call ID already belongs to a different routing choice.")
        return {"route": route, "sort": ROUTES[route]}

    @staticmethod
    def _pending(db, limit, role="operator", *, image_slots=MAX_TURN_IMAGES):
        if limit < 1:
            raise ValueError("Batch size must be positive")
        rows = db.execute(EVENT_SELECT + """JOIN inbox i ON e.id=i.event_id
            WHERE i.batch_id IS NULL AND e.role=? ORDER BY i.event_id LIMIT ?""", (role, limit))
        messages = []
        for row in rows:
            event = _event(db, row)
            if len(event.images) > image_slots:
                break
            image_slots -= len(event.images)
            messages.append(event)
        return tuple(messages)

    @staticmethod
    def _validate_minutes(minutes):
        if type(minutes) is not int or (minutes != 0 and not 5 <= minutes <= 10080):
            raise ValueError("Choose 0 to pause heartbeats or a whole number of minutes from 5 to 10080")

    def initialize_heartbeat(self, minutes):
        """Set a new installation's default once. A restart preserves Lantern's chosen cadence."""
        self._validate_minutes(minutes)
        with self._connect(write=True) as db:
            db.execute("""UPDATE scheduler SET heartbeat_minutes=?, next_heartbeat=?
                WHERE id=1 AND heartbeat_minutes IS NULL""",
                       (minutes, self.clock().isoformat() if minutes else None))

    def _schedule_change(self, call_id, request, change):
        fingerprint = json.dumps(request, ensure_ascii=False)
        with self._connect(write=True) as db:
            if call_id is not None:
                if not isinstance(call_id, str) or not call_id.strip():
                    raise ValueError("A schedule change needs a nonempty tool call ID")
                row = db.execute("SELECT request,result FROM schedule_calls WHERE id=?", (call_id,)).fetchone()
                if row:
                    if row["request"] != fingerprint:
                        raise ValueError("This tool call ID already belongs to a different schedule change")
                    return json.loads(row["result"])
            result = change(db)
            if call_id is not None:
                db.execute("INSERT INTO schedule_calls(id,request,result) VALUES(?,?,?)",
                           (call_id, fingerprint, json.dumps(result, ensure_ascii=False)))
            return result

    def configure_heartbeat(self, minutes, *, call_id=None):
        self._validate_minutes(minutes)

        def change(db):
            due = (self.clock() + timedelta(minutes=minutes)).isoformat() if minutes else None
            db.execute("UPDATE scheduler SET heartbeat_minutes=?,next_heartbeat=? WHERE id=1", (minutes, due))
            return {"heartbeat_minutes": minutes, "next_heartbeat": due}

        result = self._schedule_change(call_id, ["heartbeat", minutes], change)
        due = result["next_heartbeat"]
        return {"next_heartbeat_central": central_time(due) if due else None, **result}

    def set_alarm(self, at_utc, note, *, call_id):
        if not isinstance(note, str) or not note.strip() or len(note) > 1000 or not isinstance(at_utc, str):
            raise ValueError("An alarm needs a timestamp and a note of 1 to 1000 characters")
        try:
            due = datetime.fromisoformat(at_utc.replace("Z", "+00:00"))
            if due.utcoffset() is None:
                raise ValueError
            due = due.astimezone(timezone.utc)
        except (ValueError, OverflowError) as exc:
            raise ValueError("Use an ISO 8601 timestamp with an explicit timezone") from exc

        def change(db):
            if due <= self.clock():
                raise ValueError("An alarm must be scheduled in the future")
            alarm_id = db.execute("INSERT INTO alarms(at_utc,note) VALUES(?,?)",
                                  (due.isoformat(), note)).lastrowid
            return {"alarm_id": alarm_id, "at_utc": due.isoformat(), "status": "pending"}

        result = self._schedule_change(call_id, ["alarm", due.isoformat(), note], change)
        return {"at_central": central_time(result["at_utc"]), **result}

    def cancel_alarm(self, alarm_id, *, call_id=None):
        if type(alarm_id) is not int or not 1 <= alarm_id <= 2**63 - 1:
            raise ValueError("Expected a positive alarm ID")

        def change(db):
            db.execute("UPDATE alarms SET status='cancelled' WHERE id=? AND status='pending'", (alarm_id,))
            row = db.execute("SELECT status FROM alarms WHERE id=?", (alarm_id,)).fetchone()
            if not row:
                raise ValueError("Unknown alarm ID")
            return {"alarm_id": alarm_id, "status": row["status"]}

        return self._schedule_change(call_id, ["cancel", alarm_id], change)

    def schedule_status(self, *, after_id=0):
        if type(after_id) is not int or not 0 <= after_id <= 2**63 - 1:
            raise ValueError("Expected a nonnegative alarm cursor")
        with self._connect() as db:
            now = self.clock().isoformat()
            row = db.execute("SELECT heartbeat_minutes,next_heartbeat,budget_until FROM scheduler WHERE id=1").fetchone()
            alarms = db.execute("""SELECT id AS alarm_id,at_utc,note FROM alarms
                WHERE status='pending' AND id>? ORDER BY id LIMIT 21""", (after_id,)).fetchall()
            due = row["next_heartbeat"]
            until = row["budget_until"] if (row["budget_until"] or "") > now else None
            return {"central_now": central_time(now), "utc_now": now, "heartbeat_minutes": row["heartbeat_minutes"],
                    "next_heartbeat_central": central_time(due) if due else None, "next_heartbeat": due,
                    "budget_until_central": central_time(until) if until else None, "budget_until": until,
                    "pending_alarms": [{"at_central": central_time(alarm["at_utc"]), **dict(alarm)} for alarm in alarms[:20]],
                    "next_after_id": alarms[19]["alarm_id"] if len(alarms) > 20 else None}

    @staticmethod
    def _assign(db, messages, batch_id, steered):
        db.executemany("UPDATE inbox SET batch_id=?,steered=? WHERE event_id=?",
                       ((batch_id, steered, msg.id) for msg in messages))

    @staticmethod
    def _active(db, batch_id):
        if not db.execute("SELECT 1 FROM batches WHERE id=? AND status='active'", (batch_id,)).fetchone():
            raise ValueError("Chat turn is no longer active")

    def claim(self, limit: int = 16, *, budget_check=None, include_scheduled=True) -> Batch | None:
        """Admit human input first, then a deferred continuation, due alarm, or one idle heartbeat."""
        with self._connect(write=True) as db:
            operator_release.process_commands(self, db)
            if db.execute("SELECT 1 FROM batches WHERE status='active'").fetchone():
                return None
            now = self.clock()
            stamp = now.isoformat()
            schedule = db.execute("SELECT * FROM scheduler WHERE id=1").fetchone()
            if (schedule["budget_until"] or "") > stamp:
                return None
            messages = self._pending(db, limit)
            if not messages and not include_scheduled:
                return None
            messages = messages or self._pending(db, 1, "system")
            alarm = None
            if not messages:
                alarm = db.execute("""SELECT id,at_utc,note FROM alarms WHERE status='pending'
                    AND at_utc<=? ORDER BY at_utc,id LIMIT 1""", (stamp,)).fetchone()
                if not alarm and (not schedule["next_heartbeat"] or schedule["next_heartbeat"] > stamp):
                    return None
            if budget_check and not budget_check():
                until = next_reset(now).isoformat()
                db.execute("UPDATE scheduler SET budget_until=? WHERE id=1", (until,))
                self._append(db, "system", f"Queued work is waiting for the daily budget reset at {central_time(until)}.")
                return None
            if not messages:
                if alarm:
                    db.execute("UPDATE alarms SET status='delivered' WHERE id=?", (alarm["id"],))
                    text = (f"Scheduled alarm #{alarm['id']} (due {central_time(alarm['at_utc'])}). "
                            "This is an automated reminder. Your saved note follows as quoted data.\n"
                            + json.dumps(alarm["note"], ensure_ascii=False))
                else:
                    due = (now + timedelta(minutes=schedule["heartbeat_minutes"])).isoformat()
                    db.execute("UPDATE scheduler SET next_heartbeat=? WHERE id=1", (due,))
                    text = "Scheduled heartbeat. You may pursue your own priorities or finish this wake without acting."
                event_id = self._append(db, "system", text)
                db.execute("INSERT INTO inbox(event_id) VALUES(?)", (event_id,))
                messages = self._pending(db, 1, "system")
            batch_id = db.execute("INSERT INTO batches(status) VALUES('active')").lastrowid
            self._assign(db, messages, batch_id, False)
            thread_id = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()[0]
            return Batch(batch_id, thread_id, messages)

    def defer_for_budget(self, batch_id, hold):
        """A local cap can block the first call or interrupt a turn after effects are possible."""
        until = hold.reset_at.astimezone(timezone.utc).isoformat()
        with self._connect(write=True) as db:
            self._active(db, batch_id)
            db.execute("UPDATE scheduler SET budget_until=? WHERE id=1", (until,))
            if hold.dispatched:
                text = (f"Scheduled continuation of turn {batch_id} after a daily budget pause. "
                        "Check the previous exchange and tool results for unfinished work. "
                        "If an action's outcome is uncertain, ask Mike before repeating it.")
                event_id = self._append(db, "system", text)
                db.execute("INSERT INTO inbox(event_id) VALUES(?)", (event_id,))
            else:
                db.execute("UPDATE inbox SET batch_id=NULL,steered=0 WHERE batch_id=?", (batch_id,))
            receipt = self._append(db, "system", f"Turn {batch_id} is waiting for the daily budget reset at {central_time(until)}.")
            db.execute("UPDATE batches SET status='deferred',outcome=? WHERE id=?", (receipt, batch_id))

    def bind_thread(self, batch_id: int, thread_id: str) -> None:
        """Commit the native thread ID before starting its first inference."""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("Missing Codex thread ID")
        with self._connect(write=True) as db:
            self._active(db, batch_id)
            changed = db.execute("""UPDATE room SET thread_id=? WHERE id=1
                AND (thread_id IS NULL OR thread_id=?)""", (thread_id, thread_id)).rowcount
            if not changed:
                raise ValueError("Codex thread differs from the persisted chat thread")

    def claim_steer(self, batch_id: int, limit: int = 16) -> tuple[Event, ...]:
        """Claim additions before attempting their delivery into the running turn."""
        with self._connect(write=True) as db:
            self._active(db, batch_id)
            operator_release.process_commands(self, db)
            used = db.execute("""SELECT COUNT(DISTINCT im.event_id) + COUNT(x.position)
                FROM inbox i JOIN message_images im ON im.event_id=i.event_id
                LEFT JOIN extra_message_images x ON x.event_id=im.event_id WHERE i.batch_id=?""",
                              (batch_id,)).fetchone()[0]
            messages = self._pending(db, limit, image_slots=MAX_TURN_IMAGES - used)
            self._assign(db, messages, batch_id, True)
            return messages

    def release_steer(self, batch_id: int, ids) -> None:
        """Requeue only additions known to have been rejected or never sent."""
        with self._connect(write=True) as db:
            self._active(db, batch_id)
            for event_id in ids:
                changed = db.execute("""UPDATE inbox SET batch_id=NULL, steered=0
                    WHERE event_id=? AND batch_id=? AND steered=1""", (event_id, batch_id)).rowcount
                if not changed:
                    raise ValueError("Event is not steering input for this turn")

    def notify(self, batch_id: int, text: str, call_id: str) -> int:
        """Publish a message from a running turn, independent of terminal attachment."""
        if not isinstance(text, str) or not text.strip() or not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("A notification needs message text and a tool call ID")
        text = trim_reply_segment(text)
        with self._connect(write=True) as db:
            known = db.execute("""SELECT e.id,e.text FROM notifications n JOIN events e ON e.id=n.event_id
                WHERE n.batch_id=? AND n.call_id=?""", (batch_id, call_id)).fetchone()
            if known:
                # A receipt issued by an older release may contain padding.
                if trim_reply_segment(known["text"]) != text:
                    raise ValueError("This notification ID already belongs to different text")
                return known["id"]
            self._active(db, batch_id)
            event_id = self._append(db, "lantern", text)
            db.execute("INSERT INTO notifications VALUES(?,?,?)", (batch_id, call_id, event_id))
            return event_id

    def _finish(self, db, batch_id, status, text):
        batch = db.execute("SELECT status,outcome FROM batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ValueError("Unknown chat turn")
        outcome = None
        if status != "complete":
            spans = []
            for (event_id,) in db.execute("SELECT event_id FROM inbox WHERE batch_id=? ORDER BY event_id", (batch_id,)):
                if spans and event_id == spans[-1][1] + 1:
                    spans[-1][1] = event_id
                else:
                    spans.append([event_id, event_id])
            ids = ", ".join(str(lo) if lo == hi else f"{lo}–{hi}" for lo, hi in spans)
            scheduled = db.execute("""SELECT 1 FROM inbox JOIN events ON events.id=inbox.event_id
                WHERE batch_id=? AND role='system' LIMIT 1""", (batch_id,)).fetchone()
            label = "input events" if scheduled else "operator messages"
            text = f"Turn {batch_id} {status} ({label} {ids}). {text}"
        elif not text.strip():
            # A tool-only turn may already have delivered its entire reply.
            outcome = db.execute("SELECT MAX(event_id) FROM notifications WHERE batch_id=?", (batch_id,)).fetchone()[0]
            if outcome is None:
                raise ValueError("Lantern returned an empty chat reply")
        if batch["status"] != "active":
            previous = db.execute("SELECT text FROM events WHERE id=?", (batch["outcome"],)).fetchone()
            if batch["status"] == status and (batch["outcome"] == outcome or previous["text"] == text):
                return batch["outcome"]
            raise ValueError("A different outcome is already recorded for this turn")
        role = "lantern" if status == "complete" else "system"
        event_id = outcome if outcome is not None else self._append(db, role, text)
        db.execute("UPDATE batches SET status=?,outcome=? WHERE id=?", (status, event_id, batch_id))
        return event_id

    def complete(self, batch_id: int, text: str) -> int:
        if not isinstance(text, str):
            raise ValueError("Chat reply must be text")
        with self._connect(write=True) as db:
            return self._finish(db, batch_id, "complete", text)

    def fail(self, batch_id: int, reason: str) -> int:
        with self._connect(write=True) as db:
            return self._finish(db, batch_id, "failed", reason)

    def recover(self) -> int:
        """Call once after obtaining the exclusive worker lock on startup."""
        with self._connect(write=True) as db:
            batch = db.execute("SELECT id FROM batches WHERE status='active'").fetchone()
            if batch is None:
                return 0
            self._finish(db, batch["id"], "interrupted",
                         "Delivery or tool effects may have occurred. Check before resubmitting.")
            return 1
