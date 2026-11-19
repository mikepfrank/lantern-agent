"""Read Luna's retained flag explanations for the operator."""

from contextlib import closing
from pathlib import Path
import sqlite3

from .supervision import DATABASE


def flag_report(state: Path, *, kind: str | None = None, limit: int = 20,
                batch: int | None = None, before_id: int | None = None) -> dict:
    """Return the most recent matching entries, paging backwards by audit ID."""
    if kind not in (None, "context", "tool"):
        raise ValueError("Choose context or tool for the flag kind")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Use a flag limit between 1 and 200")

    filters = []
    values = []
    if kind == "tool":
        filters.append("kind IN ('tool', 'unregistered_tool')")
    elif kind == "context":
        filters.append("kind = 'context'")
    if batch is not None:
        if type(batch) is not int or not 1 <= batch < 2**63:
            raise ValueError("Use a positive batch ID in the SQLite integer range")
        filters.append("batch_id = ?")
        values.append(batch)
    if before_id is not None:
        if type(before_id) is not int or not 1 <= before_id < 2**63:
            raise ValueError("Use a positive flag ID in the SQLite integer range")
        filters.append("id < ?")
        values.append(before_id)

    rows = []
    path = state / DATABASE
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='luna_flags'").fetchone():
                query = """SELECT id, at AS at_utc, thread AS thread_id, batch_id, sequence AS request_sequence,
                                  kind, tool, call_id, evidence, summary FROM luna_flags"""
                if filters:
                    query += " WHERE " + " AND ".join(filters)
                rows = db.execute(query + " ORDER BY id DESC LIMIT ?", [*values, limit + 1]).fetchall()

    has_more = len(rows) > limit
    return {
        "coverage": "Explanations are available here from the introduction of Luna flag logging onward.",
        "flags": [dict(row) for row in rows[:limit]],
        "has_more": has_more,
        "next_before_id": rows[limit - 1]["id"] if has_more else None,
    }
