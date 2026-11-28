"""Read the bound room's original records and incrementally index eligible native tool events."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .compaction_annotations import annotation_text
from .history_calls import call_event_stream


def utc_stamp(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Supervision's SQLite timestamps use UTC without an offset.
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _attach(db, path, alias):
    db.execute(f"ATTACH DATABASE ? AS {alias}", (path.resolve().as_uri() + "?mode=ro",))


def _refresh_calls(db, state, thread):
    db.execute("""CREATE TABLE IF NOT EXISTS native_records (
        ref TEXT PRIMARY KEY, at TEXT NOT NULL, source TEXT NOT NULL, tool TEXT NOT NULL,
        text TEXT NOT NULL, call_id TEXT NOT NULL, seq INTEGER NOT NULL, native_turn TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS message_anchors (
        native_turn TEXT NOT NULL, item_id TEXT NOT NULL, PRIMARY KEY(native_turn,item_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS checkpoint (
        id INTEGER PRIMARY KEY CHECK(id=1), thread TEXT, identity TEXT, position INTEGER, native_turn TEXT)""")
    db.execute("BEGIN IMMEDIATE")
    with call_event_stream(state / "codex", thread) as stream:
        previous = db.execute("SELECT thread,identity,position,native_turn FROM checkpoint").fetchone()
        position = 0
        native_turn = None
        if (previous and previous["thread"] == thread and previous["identity"] == stream.identity
                and previous["position"] <= stream.size):
            position = previous["position"]
            native_turn = previous["native_turn"]
        else:
            db.execute("DELETE FROM native_records")
            db.execute("DELETE FROM message_anchors")

        def lookup_tool(call_id):
            row = db.execute("SELECT tool FROM native_records WHERE ref=?", ("call:" + call_id,)).fetchone()
            return row["tool"] if row else None

        for position, event in stream.read(position, lookup_tool, native_turn):
            if event is None:
                continue
            if event["source"] == "anchor":
                db.execute("INSERT OR IGNORE INTO message_anchors VALUES(?,?)", (event["native_turn"], event["item_id"]))
            else:
                db.execute("""INSERT OR IGNORE INTO native_records(ref,at,source,tool,text,call_id,seq,native_turn)
                    VALUES (?,?,?,?,?,?,?,?)""", (event["ref"], utc_stamp(event["at"]), event["source"],
                        event["tool"], event["text"], event["call_id"], position, event["native_turn"]))
        db.execute("INSERT OR REPLACE INTO checkpoint VALUES(1,?,?,?,?)",
                   (thread, stream.identity, position, stream.native_turn))
    db.commit()


_ROOM = """SELECT 'room:' || e.id AS ref, stamp(e.at) AS at,
    CASE WHEN e.role='system' THEN 'service' ELSE e.role END AS source,
    CASE WHEN e.role='system' THEN 'service' ELSE e.role END AS author, NULL AS tool, e.text AS text,
    COALESCE(i.batch_id,b.id,rb.batch_id,n.batch_id,compact.batch_id) AS turn,
    n.call_id AS call_id, 0 AS kind, e.id AS seq,
    release.concern_id AS release_id, release.released_at AS release_at
    FROM chat.events e LEFT JOIN chat.inbox i ON i.event_id=e.id
    LEFT JOIN chat.batches b ON b.outcome=e.id
    LEFT JOIN chat.reply_blocks rb ON rb.event_id=e.id
    LEFT JOIN chat.notifications n ON n.event_id=e.id
    LEFT JOIN room_compactions compact ON compact.event_id=e.id
    LEFT JOIN chat.operator_concern_releases release ON
        (release.command_event_id=e.id OR release.receipt_event_id=e.id) AND
        release.thread=(SELECT thread_id FROM chat.room WHERE id=1)
    WHERE e.role IN ('operator','lantern','system') AND (i.event_id IS NULL OR i.batch_id IS NOT NULL)"""

_RULINGS = """SELECT 'ruling:' || c.id || ':' || r.version, stamp(r.at), 'ruling',
    CASE WHEN r.status='retired' AND receipt.text LIKE 'Mike retired context concern #' || c.id || '. %'
         THEN 'operator' WHEN recovery.version IS NOT NULL THEN 'service' ELSE 'reviewer' END,
    a.tool, 'Concern #' || c.id || ' v' || r.version || ' (' || r.status || ')' || char(10) ||
        r.reason || char(10) || 'Recorded tools: ' || r.tools ||
        CASE WHEN a.decision IS NULL THEN '' ELSE char(10) || 'Decision on ' || a.tool || ': ' ||
            a.decision || char(10) || a.reason END,
    a.turn, a.call_id, 1, r.rowid, release.concern_id, release.released_at
    FROM supervision.concern_rulings r JOIN supervision.context_concerns c ON c.id=r.concern_id
    LEFT JOIN supervision.concern_ruling_actions a ON a.concern_id=c.id AND a.version=r.version
    LEFT JOIN supervision.recovered_concern_questions recovery ON recovery.concern_id=c.id AND recovery.version=r.version
    LEFT JOIN chat.operator_concern_releases release ON release.concern_id=c.id AND release.thread=c.thread
        AND r.status='retired'
    LEFT JOIN chat.events receipt ON receipt.id=release.receipt_event_id AND receipt.role='system'
    WHERE c.thread=(SELECT thread_id FROM chat.room WHERE id=1)"""

_CALLS = """SELECT n.ref,n.at,n.source,CASE n.source WHEN 'call' THEN 'lantern' ELSE 'tool' END,
    n.tool,n.text,COALESCE(links.turn,group_links.turn),n.call_id,2,n.seq,NULL,NULL
    FROM native_records n LEFT JOIN turn_links links ON links.call_id=n.call_id
    LEFT JOIN native_turn_links group_links ON group_links.native_turn=n.native_turn
    WHERE (SELECT thread FROM checkpoint WHERE id=1)=(SELECT thread_id FROM chat.room WHERE id=1)"""

_ANNOTATIONS = """SELECT 'annotation:' || a.id,stamp(a.occurred_at),'service','service',NULL,
    annotation_text(a.occurred_at),NULL,NULL,3,a.id,NULL,NULL,stamp(a.recorded_at)
    FROM chat.compaction_annotations a WHERE a.thread_id=(SELECT thread_id FROM chat.room WHERE id=1)
    AND stamp(a.recorded_at)<=annotation_cutoff()"""


@contextmanager
def history_records(state, thread, index_dir=None, *, annotation_cutoff="9999"):
    state = Path(state)
    chat = state / "chat.sqlite"
    # Check the room before opening or populating a private index.
    with closing(sqlite3.connect(chat.resolve().as_uri() + "?mode=ro", uri=True)) as room:
        binding = room.execute("SELECT thread_id FROM room WHERE id=1").fetchone()
    if not thread or not binding or binding[0] != thread:
        raise ValueError("History is available only in its bound private room.")

    directory = Path(index_dir) if index_dir is not None else state / "history-index"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "calls-v1.sqlite", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.create_function("stamp", 1, utc_stamp, deterministic=True)
        warnings = []
        sources = [_ROOM]
        try:
            _refresh_calls(db, state, thread)
        except (ValueError, OSError, sqlite3.Error):
            db.rollback()
            warnings.append("Native tool history is unavailable for this lookup.")
        else:
            sources.append(_CALLS)
        _attach(db, chat, "chat")
        # Make the room binding check, all records and their references agree on one snapshot.
        db.execute("BEGIN")
        if db.execute("SELECT thread_id FROM chat.room WHERE id=1").fetchone()[0] != thread:
            raise ValueError("The private room changed during lookup. Retry in the current room.")
        # Allow exact lookup on read-only room snapshots from before Service recorded compactions.
        has_compactions = db.execute("SELECT 1 FROM chat.sqlite_master WHERE type='table' AND name='compaction_items'").fetchone()
        if has_compactions:
            db.execute("""CREATE TEMP VIEW room_compactions AS SELECT event_id,batch_id FROM chat.compaction_items
                WHERE thread_id=(SELECT thread_id FROM chat.room WHERE id=1) AND event_id IS NOT NULL""")
        else:
            db.execute("CREATE TEMP VIEW room_compactions(event_id,batch_id) AS SELECT NULL,NULL WHERE 0")
        links = ["SELECT call_id,batch_id FROM chat.notifications"]
        if (state / "supervision.sqlite").is_file():
            _attach(db, state / "supervision.sqlite", "supervision")
            sources.append(_RULINGS)
            links.append("""SELECT call_id,batch_id FROM supervision.luna_flags
                WHERE thread=(SELECT thread_id FROM chat.room WHERE id=1)""")
        else:
            warnings.append("The concern ruling ledger is unavailable for this lookup.")
        if (state / "review-costs.sqlite").is_file():
            _attach(db, state / "review-costs.sqlite", "review")
            if {"thread_id", "tool_call_id", "batch_id"} <= {
                    row["name"] for row in db.execute("PRAGMA review.table_info(calls)")}:
                links.append("""SELECT tool_call_id,batch_id FROM review.calls
                    WHERE thread_id=(SELECT thread_id FROM chat.room WHERE id=1)""")
        db.execute("""CREATE TEMP TABLE turn_links(call_id TEXT PRIMARY KEY, turn INTEGER NOT NULL)""")
        db.execute(f"""INSERT INTO turn_links SELECT call_id,min(batch_id) FROM ({" UNION ALL ".join(links)})
            WHERE call_id IS NOT NULL AND batch_id IS NOT NULL GROUP BY call_id
            HAVING count(DISTINCT batch_id)=1""")
        if _CALLS in sources:
            db.execute("""CREATE TEMP TABLE native_turn_links(native_turn TEXT PRIMARY KEY,turn INTEGER)""")
            db.execute("""INSERT INTO native_turn_links SELECT native_turn,min(turn) FROM (
                SELECT n.native_turn,l.turn FROM native_records n JOIN turn_links l USING(call_id)
                UNION ALL SELECT m.native_turn,r.batch_id FROM message_anchors m
                    JOIN chat.reply_blocks r ON r.item_id=m.item_id)
                WHERE native_turn IS NOT NULL GROUP BY native_turn HAVING count(DISTINCT turn)=1""")
        originals = " UNION ALL ".join(sources)
        records = f"SELECT *,NULL AS recorded_at FROM ({originals})"
        if db.execute("SELECT 1 FROM chat.sqlite_master WHERE type='table' AND name='compaction_annotations'").fetchone():
            db.create_function("annotation_text", 1, annotation_text, deterministic=True)
            db.create_function("annotation_cutoff", 0, lambda: annotation_cutoff, deterministic=True)
            records += " UNION ALL " + _ANNOTATIONS
        db.execute("CREATE TEMP VIEW history AS " + records)
        yield db, warnings
