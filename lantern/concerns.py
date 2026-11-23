"""Host-owned context findings and append-only Opus rulings in supervision.sqlite."""

import json
import re


def initialize(db):
    """Import legacy findings idempotently. The caller owns the transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS context_concerns (
        id INTEGER PRIMARY KEY, thread TEXT NOT NULL, sequence INTEGER NOT NULL,
        evidence TEXT NOT NULL, summary TEXT NOT NULL, related_id INTEGER REFERENCES context_concerns(id),
        legacy INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')))""")
    db.execute("""CREATE INDEX IF NOT EXISTS context_concerns_thread
                  ON context_concerns(thread, sequence, id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS concern_rulings (
        concern_id INTEGER NOT NULL REFERENCES context_concerns(id), version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('watch', 'question', 'retired')),
        reason TEXT NOT NULL, tools TEXT NOT NULL, at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
        PRIMARY KEY(concern_id, version))""")
    db.execute("""CREATE TABLE IF NOT EXISTS concern_ruling_actions (
        concern_id INTEGER NOT NULL, version INTEGER NOT NULL, tool TEXT NOT NULL,
        call_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('allow','deny','ask_operator')),
        reason TEXT NOT NULL, turn INTEGER, PRIMARY KEY(concern_id,version),
        FOREIGN KEY(concern_id,version) REFERENCES concern_rulings(concern_id,version))""")
    db.execute("""CREATE TABLE IF NOT EXISTS concern_repeats (
        thread TEXT NOT NULL, sequence INTEGER NOT NULL, concern_id INTEGER NOT NULL REFERENCES context_concerns(id),
        seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')), PRIMARY KEY(thread,sequence))""")
    db.execute("CREATE INDEX IF NOT EXISTS concern_repeats_case ON concern_repeats(concern_id,sequence)")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='findings'").fetchone():
        # Legacy 'heard' only means Opus saw a report alongside a tool. It gives
        # no disposition. Retain each distinct legacy report once, also after a
        # rollback to an older worker that appends new evidence to a legacy row.
        db.execute("""INSERT INTO context_concerns(thread,sequence,evidence,summary,legacy)
            SELECT f.thread,f.sequence,f.evidence,f.summary,1 FROM findings AS f
            WHERE NOT EXISTS (SELECT 1 FROM context_concerns AS c WHERE c.legacy=1
                AND c.thread=f.thread AND c.sequence=f.sequence
                AND c.evidence=f.evidence AND c.summary=f.summary)
            ORDER BY f.thread,f.sequence""")


def create(db, thread, sequence, evidence, summary, related_id=None):
    """A fresh report receives its own ID even if an earlier incident is related."""
    if not isinstance(thread, str) or not thread or type(sequence) is not int or sequence < 1:
        raise ValueError("Invalid context concern origin")
    if not all(isinstance(part, str) and part.strip() for part in (evidence, summary)):
        raise ValueError("A context concern needs both evidence and a summary")
    if related_id is not None and (type(related_id) is not int or not 0 < related_id < 2**63):
        raise ValueError("Invalid related concern ID")
    cursor = db.execute("""INSERT INTO context_concerns(thread,sequence,evidence,summary,related_id)
        SELECT ?,?,?,?,? WHERE ? IS NULL OR EXISTS
        (SELECT 1 FROM context_concerns WHERE id=? AND thread=?)""",
        (thread, sequence, evidence, summary, related_id, related_id, related_id, thread))
    if not cursor.rowcount:
        raise ValueError("Related concern is outside this thread")
    return cursor.lastrowid


_LATEST = """FROM context_concerns AS c LEFT JOIN concern_rulings AS r
    ON r.concern_id=c.id AND r.version=(
        SELECT MAX(version) FROM concern_rulings WHERE concern_id=c.id)"""
_COLUMNS = """c.id, c.sequence, c.evidence, c.summary, c.related_id, COALESCE(r.version,0) AS version,
    COALESCE(r.status,'pending') AS status, COALESCE(r.reason,'') AS reason, COALESCE(r.tools,'[]') AS tools"""
_ACTION_JOIN = "LEFT JOIN concern_ruling_actions AS a ON a.concern_id=c.id AND a.version=r.version"
_ACTION_COLUMN = """CASE WHEN a.tool IS NOT NULL THEN json_object(
    'tool',a.tool,'call_id',a.call_id,'decision',a.decision,'reason',a.reason,'turn',a.turn) END AS paired_action"""
_SELECT_ACTIONS = f"SELECT {_COLUMNS}, {_ACTION_COLUMN} {_LATEST} {_ACTION_JOIN}"


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    for row in cursor:
        receipt = dict(zip(columns, row))
        receipt["tools"] = json.loads(receipt["tools"])
        paired = receipt.pop("paired_action", None)
        if paired:
            action = json.loads(paired)
            action["call_id"] = _excerpt(action["call_id"], 128)
            action["reason"] = _excerpt(action["reason"], 300)
            receipt["action"] = action
        yield receipt


def get(db, thread, id, *, include_action=False):
    """Full latest receipt for a concern in this thread, including retired findings."""
    # Operator commands can read a pre-migration ledger before the worker starts.
    select = _SELECT_ACTIONS if include_action else f"SELECT {_COLUMNS} {_LATEST}"
    cursor = db.execute(select + " WHERE c.thread=? AND c.id=?", (thread, id))
    return next(_rows(cursor), None)


def repeat_seen(db, thread, sequence, id):
    """Record a context scan that recognized an old incident without issuing a fresh ruling."""
    if type(sequence) is not int or not 0 < sequence < 2**63 or type(id) is not int or not 0 < id < 2**63:
        raise ValueError("Invalid concern repeat reference")
    cursor = db.execute("""INSERT INTO concern_repeats(thread,sequence,concern_id)
        SELECT ?,?,c.id FROM context_concerns AS c WHERE c.id=? AND c.thread=?
        ON CONFLICT(thread,sequence) DO NOTHING""", (thread, sequence, id, thread))
    if cursor.rowcount:
        return True
    prior = db.execute("SELECT concern_id FROM concern_repeats WHERE thread=? AND sequence=?",
                       (thread, sequence)).fetchone()
    if prior is None or prior[0] != id:
        raise ValueError("Context repeat has no matching concern in this thread and scan")
    return False


def list_open(db, thread, tool=None):
    """Return all matching current concerns, with full text for adjudication."""
    cursor = db.execute(f"""SELECT {_COLUMNS} {_LATEST}
        WHERE c.thread=? AND COALESCE(r.status,'pending') != 'retired' ORDER BY c.id""", (thread,))
    return [row for row in _rows(cursor)
            if tool is None or row["status"] == "pending" or "*" in row["tools"] or tool in row["tools"]]


def link_action(db, concern_id, version, *, tool, call_id, decision, reason, turn):
    """Save the other half of a combined ruling in the same transaction as its disposition."""
    cursor = db.execute("""INSERT INTO concern_ruling_actions
        (concern_id,version,tool,call_id,decision,reason,turn)
        SELECT concern_id,version,?,?,?,?,? FROM concern_rulings WHERE concern_id=? AND version=?""",
        (tool, call_id, decision, reason, turn, concern_id, version))
    if not cursor.rowcount:
        raise ValueError("The associated concern ruling does not exist")


def _excerpt(value, limit):
    if len(value) <= limit:
        return value
    tail = limit // 3
    return value[:limit - tail - 5] + " […] " + value[-tail:]


def review_receipts(db, thread, referenced_ids, *, include_recent=False):
    """Latest rulings for named concerns, optionally supplemented with up to two recent rulings."""
    ids = []
    for number in referenced_ids:
        if type(number) is int and 0 < number < 2**63 and number not in ids:
            ids.append(number)
            if len(ids) == 4:
                break
    if not ids and not include_recent:
        return []
    marks = ",".join("?" for _ in ids) or "NULL"
    sql = f"""SELECT c.id,r.version,r.status,r.tools,r.reason, {_ACTION_COLUMN} {_LATEST} {_ACTION_JOIN}
              WHERE c.thread=? AND r.version IS NOT NULL AND (c.id IN ({marks}) OR ?)
              ORDER BY CASE WHEN c.id IN ({marks}) THEN 0 ELSE 1 END, r.at DESC,r.rowid DESC LIMIT 4"""
    rows = list(_rows(db.execute(sql, (thread, *ids, include_recent, *ids))))
    named = {row["id"]: row for row in rows if row["id"] in ids}
    recent = [row for row in rows if row["id"] not in named]
    receipts = [named[number] for number in ids if number in named] + recent[:2]
    for receipt in receipts:
        receipt["reason"] = _excerpt(receipt["reason"], 400)
        scope = receipt["tools"]
        if len(scope) > 8:
            receipt["tools"] = scope[:8]
            receipt["tools_omitted"] = len(scope) - 8
    return receipts


def history(db, thread, limit=16):
    """Bound the context scan's receipts; list_open separately gates every live scope."""
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("Use a concern history limit between 1 and 32")
    cursor = db.execute(f"""{_SELECT_ACTIONS}
        LEFT JOIN concern_repeats p ON p.concern_id=c.id AND p.sequence=(
            SELECT MAX(sequence) FROM concern_repeats WHERE concern_id=c.id)
        WHERE c.thread=?
        ORDER BY MAX(COALESCE(r.at,c.created_at),COALESCE(p.seen_at,'')) DESC,
                 MAX(c.sequence,COALESCE(p.sequence,0)) DESC,c.id DESC LIMIT ?""", (thread, limit))
    receipts = list(_rows(cursor))
    for row in receipts:
        for field in ("evidence", "summary", "reason"):
            if len(row[field]) > 600:
                row[field] = row[field][:380] + " […] " + row[field][-215:]
    return receipts


def validate_scope(decision, tools):
    if (not isinstance(tools, list) or any(not isinstance(tool, str) or
            not (tool == "*" or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", tool)) for tool in tools)
            or len(set(tools)) != len(tools) or ("*" in tools and len(tools) != 1)
            or bool(tools) != (decision != "retire")):
        raise ValueError("Invalid tool scope for this context ruling")


def record(db, thread, id, expected_version, decision, reason, tools):
    """Append one ruling only if this version is still current and active."""
    statuses = {"retire": "retired", "watch": "watch", "ask_operator": "question"}
    if decision not in statuses or not isinstance(reason, str) or not reason.strip():
        raise ValueError("Invalid context ruling")
    if type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1:
        raise ValueError("Invalid concern version")
    if type(id) is not int or not 0 < id < 2**63:
        raise ValueError("Invalid concern ID")
    validate_scope(decision, tools)
    cursor = db.execute(f"""INSERT INTO concern_rulings(concern_id,version,status,reason,tools)
        SELECT c.id,?,?,?,? {_LATEST}
        WHERE c.thread=? AND c.id=? AND COALESCE(r.version,0)=? AND COALESCE(r.status,'pending')!='retired'""",
        (expected_version + 1, statuses[decision], reason, json.dumps(tools), thread, id, expected_version))
    return bool(cursor.rowcount)
