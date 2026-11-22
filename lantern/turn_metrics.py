"""Qwen cost per finished room turn, including the spend on failed turns."""

from collections import defaultdict
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3

from .budget import UNITS_PER_DOLLAR, routing_receipt_sql
from .routing import ROUTES, safe_provider


def _group_label(values):
    labels = set(values)
    if not labels or None in labels:
        return "unknown"
    return labels.pop() if len(labels) == 1 else "mixed"


def _summary(turns, ambiguous_receipts):
    succeeded = sum(status == "complete" for status, _ in turns)
    amounts = [r["actual"] for _, rows in turns for r in rows]
    unlinked = sum(not rows for _, rows in turns)
    unsettled = amounts.count(None)
    settled = Decimal(sum(cost for cost in amounts if cost is not None)) / UNITS_PER_DOLLAR
    per_success = str(settled / succeeded) if succeeded else None
    partial = bool(unlinked or unsettled or ambiguous_receipts)
    result = {"successful_turns": succeeded, "failed_turns": len(turns) - succeeded,
              "settled_qwen_usd": str(settled), "unsettled_calls": unsettled,
              "turns_without_receipts": unlinked, "usd_per_successful_turn": None if partial else per_success}
    if partial and succeeded:
        result["minimum_usd_per_successful_turn"] = per_success
    return result


def room_turn_metrics(state: Path, start_day: str, end_day: str, *, batch: int | None = None,
                      provider_limit: int = 8):
    """Select complete/failed batches by UTC outcome-event date, gathering all linked calls across dates."""
    chat, ledger = state / "chat.sqlite", state / "usage.sqlite"
    if not chat.is_file() or not ledger.is_file():
        return None
    with closing(sqlite3.connect(chat.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ? AS billing", (ledger.resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        billing = {row[0] for row in db.execute("SELECT name FROM billing.sqlite_master WHERE type='table'")}
        if not {"room", "batches", "events"} <= tables or not {"requests", "request_details"} <= billing:
            return None
        detail_fields = {row[1] for row in db.execute("PRAGMA billing.table_info(request_details)")}
        if not {"id", "batch_id", "thread"} <= detail_fields:
            return None
        room = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()
        if not room or not room[0]:
            return None
        batches = {}
        earliest = start_day
        source_day = "MIN(date(input.at))" if "inbox" in tables else "NULL"
        source_join = ("LEFT JOIN inbox i ON i.batch_id=b.id LEFT JOIN events input ON input.id=i.event_id"
                       if "inbox" in tables else "")
        for batch_id, status, first_input in db.execute(f"""SELECT b.id,b.status,{source_day}
                FROM batches b JOIN events e ON e.id=b.outcome {source_join}
                WHERE b.status IN ('complete','failed') AND date(e.at) BETWEEN ? AND ?
                AND (? IS NULL OR b.id=?) GROUP BY b.id""", (start_day, end_day, batch, batch)):
            batches[batch_id] = status
            earliest = min(earliest, first_input or start_day)
        receipts = defaultdict(list)
        ambiguous = 0
        if batches:
            fields, joins = routing_receipt_sql(db, {"billing." + name for name in billing}, "billing.",
                                                only={"route", "provider"})
            rows = db.execute(f"""SELECT d.batch_id,r.actual,r.day {fields} FROM billing.request_details d
                LEFT JOIN billing.requests r USING(id) {joins}
                WHERE d.thread=? AND d.batch_id BETWEEN ? AND ?""", (room[0], min(batches), max(batches)))
            for row in rows:
                if row["batch_id"] in batches:
                    receipts[row["batch_id"]].append(dict(row))
                    earliest = min(earliest, row["day"] or start_day)
            # Older requests might lack the identifiers needed to establish that they belong elsewhere.
            # A lower bound on spend is still useful; it cannot yield an exact average.
            ambiguous, = db.execute("""SELECT COUNT(*) FROM billing.requests r
                LEFT JOIN billing.request_details d USING(id)
                WHERE r.day BETWEEN ? AND ? AND
                    ((d.thread IS NULL AND (d.batch_id IS NULL OR d.batch_id BETWEEN ? AND ?))
                     OR (d.thread=? AND d.batch_id IS NULL))""",
                (earliest, end_day, min(batches), max(batches), room[0])).fetchone()

    all_turns = []
    by_route, by_provider = defaultdict(list), defaultdict(list)
    for batch_id, status in batches.items():
        rows = receipts[batch_id]
        turn = status, rows
        route = _group_label(r.get("route") if r.get("route") in ROUTES else None for r in rows)
        provider = _group_label(safe_provider(r.get("provider")) for r in rows)
        all_turns.append(turn)
        by_route[route].append(turn)
        by_provider[provider, route].append(turn)
    ranked = sorted(by_provider.items(), key=lambda item: (-len(item[1]), item[0]))
    return {"overall": _summary(all_turns, ambiguous),
            "by_route": {route: _summary(turns, ambiguous) for route, turns in by_route.items()},
            "by_provider_and_route": [{"provider": provider, "route": route, **_summary(turns, ambiguous)}
                                      for (provider, route), turns in ranked[:provider_limit]],
            "omitted_provider_turns": sum(len(turns) for _, turns in ranked[provider_limit:]),
            "unattributed_requests_in_period": ambiguous}
