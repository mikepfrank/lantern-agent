"""Process explicit operator commands in the room worker, before model admission."""

import json
import re

from . import concerns


SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_concern_releases (
    thread TEXT NOT NULL, concern_id INTEGER NOT NULL, prior_tools TEXT NOT NULL,
    reason TEXT NOT NULL, released_at TEXT NOT NULL,
    command_event_id INTEGER UNIQUE NOT NULL REFERENCES events(id),
    source_event_id INTEGER REFERENCES events(id),
    receipt_event_id INTEGER UNIQUE NOT NULL REFERENCES events(id),
    PRIMARY KEY(thread,concern_id));
"""
COMMAND = "/release-concern"
USAGE = "Use /release-concern N, optionally followed by a short reason or --cite EVENT."
_COMMAND = re.compile(r"/release-concern[ \t]+#?([1-9][0-9]{0,18})(?:[ \t]+([^\r\n]{1,600}))?")
_CITE = re.compile(r"--cite[ \t]+([1-9][0-9]{0,18})")


def process_commands(store, db):
    """Caller holds the chat write lock. Commit ledger, receipt and queue changes together."""
    commands = db.execute("""SELECT e.id,e.text,im.event_id AS image FROM inbox i
        JOIN events e ON e.id=i.event_id LEFT JOIN message_images im ON im.event_id=e.id
        WHERE i.batch_id IS NULL AND e.role='operator' AND substr(e.text,1,?)=?
        ORDER BY e.id""", (len(COMMAND), COMMAND)).fetchall()
    commands = [row for row in commands if row["text"].split(maxsplit=1)[0] == COMMAND]
    if not commands:
        return
    thread = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()[0]
    path = store.path.with_name("supervision.sqlite")
    available = False
    if thread and path.is_file():
        # Both on-disk databases use SQLite's rollback journal, enabling an atomic
        # multi-database commit. The web process never needs access to this file.
        db.execute("ATTACH DATABASE ? AS supervision", (str(path),))
        available = db.execute("""SELECT COUNT(*) FROM supervision.sqlite_schema WHERE
            type='table' AND name IN ('context_concerns','concern_rulings')""").fetchone()[0] == 2
    for row in commands:
        try:
            match = _COMMAND.fullmatch(row["text"].strip())
            if not match or row["image"] or int(match[1]) >= 2**63:
                raise ValueError(USAGE)
            if not available:
                raise ValueError("The room has no initialized concern ledger.")
            receipt = _release(store, db, thread, row["id"], int(match[1]), (match[2] or "").strip())
        except ValueError as error:
            store._append(db, "system", f"Release command at event #{row['id']} was declined. {error}")
        else:
            if receipt is not None:
                db.execute("INSERT INTO inbox(event_id) VALUES(?)", (receipt,))
        db.execute("DELETE FROM inbox WHERE event_id=? AND batch_id IS NULL", (row["id"],))


def _release(store, db, thread, command_event_id, concern_id, reason):
    source_id = None
    if reason.startswith("--"):
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
