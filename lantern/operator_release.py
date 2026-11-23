"""Process explicit operator commands in the room worker, before model admission."""

import json
import re
import sqlite3
from time import monotonic

from . import concerns


SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_concern_releases (
    thread TEXT NOT NULL, concern_id INTEGER NOT NULL, prior_tools TEXT NOT NULL,
    reason TEXT NOT NULL, released_at TEXT NOT NULL,
    command_event_id INTEGER UNIQUE NOT NULL REFERENCES events(id),
    source_event_id INTEGER REFERENCES events(id),
    receipt_event_id INTEGER UNIQUE NOT NULL REFERENCES events(id),
    PRIMARY KEY(thread,concern_id));
CREATE TABLE IF NOT EXISTS room_concern_snapshots (
    thread TEXT PRIMARY KEY, as_of TEXT NOT NULL, items TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sidebar_concern_releases (
    thread TEXT NOT NULL, concern_id INTEGER NOT NULL, version INTEGER NOT NULL,
    event_id INTEGER UNIQUE NOT NULL REFERENCES events(id), PRIMARY KEY(thread,concern_id,version));
"""
COMMAND = "/release-concern"
USAGE = "Use /release-concern N, optionally followed by a short reason or --cite EVENT."
_COMMAND = re.compile(r"/release-concern[ \t]+#?([1-9][0-9]{0,18})(?:[ \t]+([^\r\n]{1,630}))?")
_CITE = re.compile(r"--cite[ \t]+([1-9][0-9]{0,18})")
_VERSION = re.compile(r"--version ([0-9]{1,19})(?: (.+))?")
MAX_REASON = 600
SNAPSHOT_INTERVAL = 15


def _snapshot(db):
    return db.execute("""SELECT r.thread_id,s.items,s.as_of FROM room r
        LEFT JOIN room_concern_snapshots s ON s.thread=r.thread_id WHERE r.id=1""").fetchone()


def sidebar(store):
    """Read only the worker's projection in chat.sqlite. The web service cannot open the ledger."""
    with store._connect() as db:
        db.execute("BEGIN")
        row = _snapshot(db)
        items = json.loads(row["items"]) if row["items"] else []
        queued = {tuple(r) for r in db.execute("""SELECT s.concern_id,s.version FROM sidebar_concern_releases s
            JOIN inbox i ON i.event_id=s.event_id WHERE s.thread=? AND i.batch_id IS NULL""", (row["thread_id"],))}
        for item in items:
            item["release_pending"] = (item["id"], item["version"]) in queued
        return {"as_of": row["as_of"], "concerns": items}


class ReleaseConflict(ValueError):
    pass


def queue_release(store, number, version, reason):
    if (type(version) is not int or not 0 <= version < 2**63 - 1 or not isinstance(reason, str)
            or len(reason) > MAX_REASON or re.search(r"[\x00-\x1f\x7f\x85\u2028\u2029]", reason)):
        raise ValueError("Supply the displayed version and an optional single-line reason up to 600 characters.")
    reason.encode("utf-8")
    # The version is persisted in the human-readable command. An older worker
    # declines the unfamiliar option rather than retiring the latest version.
    command = f"{COMMAND} {number} --version {version}" + (f" {reason.strip()}" if reason.strip() else "")
    with store._connect(write=True) as db:
        row = _snapshot(db)
        key = (row["thread_id"], number, version)
        prior = db.execute("""SELECT e.id,e.text FROM sidebar_concern_releases s JOIN events e ON e.id=s.event_id
            WHERE s.thread=? AND s.concern_id=? AND s.version=?""", key).fetchone()
        if prior:
            if prior["text"] != command:
                raise ReleaseConflict("A release with a different reason was already submitted for this version.")
            return prior["id"]
        if row["as_of"] is None:
            raise ReleaseConflict("The worker has not published a concern list. Refresh before releasing.")
        item = next((i for i in json.loads(row["items"]) if i["id"] == number), None)
        if item is None:
            raise KeyError(number)
        if item["version"] != version:
            raise ReleaseConflict("The concern changed. Review its current ruling before releasing.")
        event_id = store._append(db, "operator", command)
        db.execute("INSERT INTO inbox(event_id) VALUES(?)", (event_id,))
        db.execute("INSERT INTO sidebar_concern_releases(thread,concern_id,version,event_id) VALUES(?,?,?,?)",
                   (*key, event_id))
        return event_id


def process_commands(store, db):
    """Caller holds the chat write lock. Commit ledger, receipt and queue changes together."""
    commands = db.execute("""SELECT e.id,e.text,im.event_id AS image FROM inbox i
        JOIN events e ON e.id=i.event_id LEFT JOIN message_images im ON im.event_id=e.id
        WHERE i.batch_id IS NULL AND e.role='operator' AND substr(e.text,1,?)=?
        ORDER BY e.id""", (len(COMMAND), COMMAND)).fetchall()
    commands = [row for row in commands if row["text"].split(maxsplit=1)[0] == COMMAND]
    now = monotonic()
    if not commands and now < store._next_concern_snapshot:
        return
    thread = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()[0]
    path = store.path.with_name("supervision.sqlite")
    available = False
    store._next_concern_snapshot = now + SNAPSHOT_INTERVAL if thread else 0
    if thread and path.is_file():
        try:
            # Both databases use rollback journals. Commands, rulings, receipts
            # and the updated sidebar snapshot commit as one worker transaction.
            # A periodic sidebar read must not hold the chat lock waiting for
            # a competing ledger writer. Commands retain the normal timeout.
            if not commands:
                db.execute("PRAGMA busy_timeout=100")
            db.execute("ATTACH DATABASE ? AS supervision", (str(path),))
            available = db.execute("""SELECT COUNT(*) FROM supervision.sqlite_schema WHERE
                type='table' AND name IN ('context_concerns','concern_rulings')""").fetchone()[0] == 2
        except sqlite3.OperationalError:
            available = None
        finally:
            if not commands:
                db.execute("PRAGMA busy_timeout=10000")
    for row in commands:
        db.execute("SAVEPOINT operator_command")
        try:
            match = _COMMAND.fullmatch(row["text"].strip())
            if not match or row["image"] or int(match[1]) >= 2**63:
                raise ValueError(USAGE)
            if available is None:
                raise ValueError("The concern ledger is temporarily unavailable. Try again once the sidebar refreshes.")
            if not available:
                raise ValueError("The room has no initialized concern ledger.")
            receipt = _release(store, db, thread, row["id"], int(match[1]), (match[2] or "").strip())
        except (ValueError, sqlite3.OperationalError) as error:
            if isinstance(error, sqlite3.OperationalError):
                if (getattr(error, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                error = "The ledger was busy. Try again once the sidebar refreshes."
            db.execute("ROLLBACK TO operator_command")
            # A new click may retry a declined request if its displayed version
            # is still valid. Preserve the original command and failure receipt.
            db.execute("DELETE FROM sidebar_concern_releases WHERE event_id=?", (row["id"],))
            store._append(db, "system", f"Release command at event #{row['id']} was declined. {error}")
        else:
            if receipt is not None:
                db.execute("INSERT INTO inbox(event_id) VALUES(?)", (receipt,))
        db.execute("RELEASE operator_command")
        db.execute("DELETE FROM inbox WHERE event_id=? AND batch_id IS NULL", (row["id"],))
    if available:
        try:
            items = json.dumps(concerns.list_open(db, thread))
        except sqlite3.OperationalError:
            # Reading the sidebar is optional. Preserve the last checked timestamp.
            return
        db.execute("""INSERT INTO room_concern_snapshots(thread,as_of,items) VALUES(?,?,?)
            ON CONFLICT(thread) DO UPDATE SET as_of=excluded.as_of,items=excluded.items""",
            (thread, store.clock().isoformat(), items))
    elif thread and available is False:
        db.execute("DELETE FROM room_concern_snapshots WHERE thread=?", (thread,))


def _release(store, db, thread, command_event_id, concern_id, reason):
    source_id = None
    expected_version = None
    guarded = _VERSION.fullmatch(reason)
    if guarded:
        expected_version = int(guarded[1])
        reason = guarded[2] or ""
        if expected_version >= 2**63 - 1 or len(reason) > MAX_REASON:
            raise ValueError("Invalid concern version or release reason.")
    elif len(reason) > MAX_REASON:
        raise ValueError(USAGE)
    elif reason.startswith("--"):
        cite = _CITE.fullmatch(reason)
        if not cite or int(cite[1]) >= 2**63:
            raise ValueError(USAGE)
        source_id = int(cite[1])
        source = db.execute("""SELECT e.text FROM events e JOIN inbox i ON i.event_id=e.id
            WHERE e.id=? AND e.role='operator' AND i.batch_id IS NOT NULL AND e.id<?""",
            (source_id, command_event_id)).fetchone()
        if not source or not re.search(rf"\bconcern\s+#?{concern_id}\b", source[0], re.I):
            raise ValueError(f"Event #{source_id} must be an earlier admitted operator message naming concern #{concern_id}.")
        reason = f"The operator cited room event #{source_id} for this retirement."
    item = concerns.get(db, thread, concern_id)
    if item is None:
        raise ValueError(f"This conversation has no context concern #{concern_id}.")
    if expected_version is not None and (item["version"] != expected_version or item["status"] == "retired"):
        raise ValueError(f"Concern #{concern_id} changed after version {expected_version} was displayed. "
                         "Review its current ruling before releasing.")
    already_retired = item["status"] == "retired"
    if already_retired:
        existing = db.execute("""SELECT receipt_event_id FROM operator_concern_releases
            WHERE thread=? AND concern_id=?""", (thread, concern_id)).fetchone()
        if existing:
            store._append(db, "system", f"Context concern #{concern_id} was already retired. "
                          f"Operator receipt #{existing[0]} is on record.")
            return None
        # A reviewer may retire the concern before the worker admits the operator's
        # command. Preserve that ruling and separately attest the operator's decision.
        last_scope = db.execute("""SELECT tools FROM supervision.concern_rulings
            WHERE concern_id=? AND status IN ('watch','question') ORDER BY version DESC LIMIT 1""",
            (concern_id,)).fetchone()
        prior_tools = last_scope[0] if last_scope else "[]"
        headline = f"Mike confirmed the retirement of context concern #{concern_id}."
        reason = f"Confirmed the existing retirement at ruling version {item['version']}." + (f" {reason}" if reason else "")
    else:
        prior_tools = json.dumps(item["tools"] or ["*"])
        headline = f"Mike retired context concern #{concern_id}."
        reason = reason or "The operator explicitly ended this watch."
    explanation = f"Direct operator release from room event #{command_event_id}. {reason}"
    if not already_retired:
        if not concerns.record(db, thread, concern_id, item["version"], "retire", explanation, []):
            raise RuntimeError("Concurrent change to the concern ledger. The command remains queued.")
    receipt = store._append(db, "system", f"{headline} {explanation} "
                            "This ruling applies only to that context concern. Individual action decisions keep their own status.")
    stamp = store.clock().isoformat()
    db.execute("""INSERT INTO operator_concern_releases
        (thread,concern_id,prior_tools,reason,released_at,command_event_id,source_event_id,receipt_event_id)
        VALUES(?,?,?,?,?,?,?,?)""", (thread, concern_id, prior_tools, reason, stamp, command_event_id, source_id, receipt))
    if db.execute("SELECT 1 FROM main.sqlite_schema WHERE name='operator_reviews'").fetchone():
        # Preserve any answer already given, including its original wake event.
        db.execute("""UPDATE operator_reviews SET status='answered',answer=COALESCE(answer,?),
            answered_at=COALESCE(answered_at,?),wake_id=COALESCE(wake_id,?),
            final_fingerprint=COALESCE(final_fingerprint,?), final_decision='retire',final_reason=?,final_tools='[]'
            WHERE thread=? AND scope=? AND tool='context_concern' AND final_decision IS NULL""",
            (explanation, stamp, receipt, f"operator-release:{command_event_id}", explanation, thread,
             f"context-concern:{concern_id}"))
    return None if already_retired else receipt
