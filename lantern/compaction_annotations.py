"""Retrospective room annotations from the bound native rollout's saved checkpoints."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .history_calls import MAX_LINE
from .local_time import central_time
from .thread_upgrade import _room_rollout


ANNOTATIONS_SCHEMA = """CREATE TABLE IF NOT EXISTS compaction_annotations (
    id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL, after_event_id INTEGER REFERENCES events(id),
    checkpoint_key TEXT NOT NULL, UNIQUE(thread_id, checkpoint_key))"""


def annotation_text(occurred_at):
    return (f"Historical context compaction at {central_time(occurred_at)}. "
            "Reconstructed from the saved checkpoint. This notice was added later "
            "and was not shown in the room at the time.")


def _stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("A checkpoint timestamp must include its UTC offset.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _checkpoints(home, thread_id, before):
    # Avoid opening a backup, sibling thread or a second copy of the same rollout.
    path = _room_rollout(home, thread_id)
    found = {}
    with path.open("rb") as file:
        header = file.readline(MAX_LINE + 1)
        if not header.endswith(b"\n") or len(header) > MAX_LINE:
            raise ValueError("Incomplete or oversized native history header.")
        record = json.loads(header)
        if record.get("type") != "session_meta" or record.get("payload", {}).get("id") != thread_id:
            raise ValueError("Native history belongs to a different thread.")
        while line := file.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise ValueError("Incomplete or oversized native history record. Retry after the turn finishes.")
            if b'"compacted"' not in line:
                continue
            record = json.loads(line)
            if record.get("type") != "compacted":
                continue
            at = _stamp(record.get("timestamp"))
            if at >= before:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Malformed native compaction checkpoint.")
            # A checkpoint's durable window ID is different from its live RPC item ID.
            # Older rollouts without window IDs can still date a compaction.
            window = payload.get("window_id")
            if isinstance(window, str) and window:
                key = "window:" + window
            else:
                key = "time:" + at
            if key in found and found[key] != at:
                raise ValueError("One native compaction window has conflicting timestamps.")
            found[key] = at
    return sorted(found.items(), key=lambda item: (item[1], item[0]))


def import_compaction_annotations(state, *, apply=False, before=None, now=None):
    """Preview or atomically import historical notices; preserve every original room event."""
    path = Path(state).resolve()
    recorded_at = _stamp(now if now is not None else datetime.now(timezone.utc))
    requested = _stamp(before) if before is not None else recorded_at
    if requested > recorded_at:
        raise ValueError("The historical compaction boundary must be in the past or present.")
    mode = "rw" if apply else "ro"
    uri = (path / "chat.sqlite").as_uri() + "?mode=" + mode
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.create_function("utc_stamp", 1, _stamp)
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        room = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()
        if not room or not room[0]:
            raise ValueError("Historical compactions are available only for a bound private room.")
        thread = room[0]
        frozen = None
        if _has_table(db, "compaction_annotation_imports"):
            frozen = db.execute("SELECT before_at FROM compaction_annotation_imports WHERE thread_id=?",
                                (thread,)).fetchone()
        if frozen and before is not None and requested < frozen[0]:
            raise ValueError("The import boundary is already frozen at " + frozen[0])
        boundary = frozen[0] if frozen else requested

        if not frozen and _has_table(db, "compaction_items"):
            # Native checkpoint times and RPC completion times can differ. The first
            # witnessed compaction's entire batch belongs to the live-notice era.
            first = db.execute("SELECT MIN(batch_id) FROM compaction_items WHERE thread_id=?", (thread,)).fetchone()[0]
            if first is not None:
                start = db.execute("""SELECT MIN(utc_stamp(e.at)) FROM inbox i JOIN events e ON e.id=i.event_id
                                   WHERE i.batch_id=? AND i.steered=0""", (first,)).fetchone()[0]
                if start is None:
                    raise ValueError("Cannot verify the beginning of the first live compaction turn.")
                boundary = min(boundary, start)

        checkpoints = _checkpoints(path / "codex", thread, boundary)
        existing = {}
        if _has_table(db, "compaction_annotations"):
            existing = {key: (at, after) for key, at, after in db.execute(
                "SELECT checkpoint_key,occurred_at,after_event_id FROM compaction_annotations WHERE thread_id=?",
                (thread,))}
        if apply:
            db.execute("""CREATE TABLE IF NOT EXISTS compaction_annotation_imports (
                thread_id TEXT PRIMARY KEY, before_at TEXT NOT NULL)""")
            db.execute(ANNOTATIONS_SCHEMA)
            db.execute("""INSERT INTO compaction_annotation_imports(thread_id,before_at) VALUES(?,?)
                       ON CONFLICT(thread_id) DO NOTHING""", (thread, boundary))
        placements = []
        to_add = 0
        for key, at in checkpoints:
            if key in existing:
                previous_at, after = existing[key]
                if previous_at != at:
                    raise ValueError("The saved checkpoint disagrees with its existing annotation.")
            else:
                row = db.execute("""SELECT id FROM events WHERE utc_stamp(at)<=?
                                  ORDER BY utc_stamp(at) DESC,id DESC LIMIT 1""", (at,)).fetchone()
                after = row[0] if row else None
                to_add += 1
                if apply:
                    db.execute("""INSERT INTO compaction_annotations
                        (thread_id,occurred_at,recorded_at,after_event_id,checkpoint_key)
                        VALUES(?,?,?,?,?)""", (thread, at, recorded_at, after, key))
            placements.append({"occurred_at": at, "after_event_id": after})
        return {"before": boundary, "checkpoints": len(checkpoints), "already_recorded": len(checkpoints) - to_add,
                "to_add": to_add, "added": to_add if apply else 0, "placements": placements}
