"""Read the private Opus response index without loading reply bodies."""

from contextlib import closing
from pathlib import Path
import sqlite3


def opus_report(state: Path, *, batch: int | None = None, limit: int = 20) -> dict:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Use a response limit between 1 and 200")
    if batch is not None and (type(batch) is not int or not 1 <= batch < 2**63):
        raise ValueError("Use a positive batch ID in the SQLite integer range")

    report = {
        "coverage": "Raw replies are available from the introduction of Opus response logging onward.",
        "responses": [],
    }
    path = state / "review-costs.sqlite"
    if not path.exists():
        return report
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='opus_responses'").fetchone():
            return report
        query = """SELECT a.request_id, c.at AS at_utc, c.thread_id, c.batch_id, c.purpose, c.tool,
                          c.tool_call_id, c.status AS billing_status, a.http_status, a.capture_status,
                          a.body_bytes, a.sha256, a.failure_code, a.validation
                   FROM opus_responses a JOIN calls c ON c.id=a.request_id WHERE c.vendor='opus'"""
        values = []
        if batch is not None:
            query += " AND c.batch_id=?"
            values.append(batch)
        for row in db.execute(query + " ORDER BY c.rowid DESC LIMIT ?", [*values, limit]):
            item = dict(row)
            item["body_path"] = (str(state / "opus-responses" / (row["request_id"] + ".body"))
                                 if row["capture_status"] in ("complete", "partial") else None)
            report["responses"].append(item)
    return report
