"""Persistent single-operator chat, ordered delivery and crash recovery."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class Event:
    id: int
    role: str
    text: str
    at: str


@dataclass(frozen=True)
class Batch:
    id: int
    thread_id: str | None
    messages: tuple[Event, ...]


class ChatStore:
    def __init__(self, state: Path):
        state.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = state / "chat.sqlite"
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS room (
                    id INTEGER PRIMARY KEY CHECK (id = 1), thread_id TEXT, read_cursor INTEGER);
                INSERT OR IGNORE INTO room(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL,
                    at TEXT NOT NULL, client_id TEXT UNIQUE);
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
            """)

    @contextmanager
    def _connect(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _append(db, role, text, client_id=None):
        now = datetime.now(timezone.utc).isoformat()
        return db.execute("INSERT INTO events(role,text,at,client_id) VALUES(?,?,?,?)",
                          (role, text, now, client_id)).lastrowid

    def submit(self, text: str, client_id: str | None = None) -> int:
        """Accept a message durably. Reusing its client ID returns its original receipt."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("The chat message is empty")
        if client_id is not None and (not isinstance(client_id, str) or not client_id.strip()):
            raise ValueError("A supplied message ID must be a nonempty string")
        with self._connect(write=True) as db:
            if client_id is not None:
                existing = db.execute("SELECT id,text FROM events WHERE client_id=?", (client_id,)).fetchone()
                if existing is not None:
                    if existing["text"] != text:
                        raise ValueError("This message ID already belongs to different text")
                    return existing["id"]
            event_id = self._append(db, "operator", text, client_id)
            db.execute("INSERT INTO inbox(event_id) VALUES(?)", (event_id,))
            return event_id

    def events(self, after: int = 0, limit: int = 100) -> list[Event]:
        if after < 0 or limit < 0:
            raise ValueError("Invalid chat event cursor or limit")
        with self._connect() as db:
            return [Event(**row) for row in db.execute(
                "SELECT id,role,text,at FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit))]

    def recent(self, limit: int = 20) -> list[Event]:
        if limit < 0:
            raise ValueError("Invalid chat history limit")
        with self._connect() as db:
            rows = db.execute("SELECT id,role,text,at FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [Event(**row) for row in reversed(rows)]

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

    @staticmethod
    def _pending(db, limit):
        if limit < 1:
            raise ValueError("Batch size must be positive")
        rows = db.execute("""SELECT e.id,e.role,e.text,e.at FROM inbox i JOIN events e ON e.id=i.event_id
            WHERE i.batch_id IS NULL ORDER BY i.event_id LIMIT ?""", (limit,))
        return tuple(Event(**row) for row in rows)

    @staticmethod
    def _assign(db, messages, batch_id, steered):
        db.executemany("UPDATE inbox SET batch_id=?,steered=? WHERE event_id=?",
                       ((batch_id, steered, msg.id) for msg in messages))

    @staticmethod
    def _active(db, batch_id):
        if not db.execute("SELECT 1 FROM batches WHERE id=? AND status='active'", (batch_id,)).fetchone():
            raise ValueError("Chat turn is no longer active")

    def claim(self, limit: int = 16) -> Batch | None:
        """Claim waiting messages in order, before handing them to Codex."""
        with self._connect(write=True) as db:
            if db.execute("SELECT 1 FROM batches WHERE status='active'").fetchone():
                return None
            messages = self._pending(db, limit)
            if not messages:
                return None
            batch_id = db.execute("INSERT INTO batches(status) VALUES('active')").lastrowid
            self._assign(db, messages, batch_id, False)
            thread_id = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()[0]
            return Batch(batch_id, thread_id, messages)

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
            messages = self._pending(db, limit)
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
        with self._connect(write=True) as db:
            known = db.execute("""SELECT e.id,e.text FROM notifications n JOIN events e ON e.id=n.event_id
                WHERE n.batch_id=? AND n.call_id=?""", (batch_id, call_id)).fetchone()
            if known:
                if known["text"] != text:
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
            text = f"Turn {batch_id} {status} (operator messages {ids}). {text}"
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
