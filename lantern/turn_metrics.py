"""Qwen cost per finished room turn, including the spend on failed turns."""

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
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


def _could_overlap(day, at, first, last):
    """Compare an admission instant, or its entire UTC day, with inclusive turn bounds."""
    if at is not None:
        return (first is None or first <= at) and (last is None or at <= last)
    if day is None:
        return True
    return (first is None or first < day + 1) and (last is None or day <= last)


@dataclass
class _Turn:
    status: str
    receipts: list
    possible: set
    verified_count: int | None


def _summary(turns):
    succeeded = sum(turn.status == "complete" for turn in turns)
    amounts = [r["actual"] for turn in turns for r in turn.receipts]
    confirmed_no_calls = sum(turn.verified_count == 0 and not turn.receipts for turn in turns)
    count_conflicts = sum(turn.verified_count is not None and turn.verified_count != len(turn.receipts)
                          for turn in turns)
    unlinked = sum(not turn.receipts for turn in turns) - confirmed_no_calls
    ambiguous = len({key for turn in turns for key in turn.possible})
    unsettled = amounts.count(None)
    settled = Decimal(sum(cost for cost in amounts if cost is not None)) / UNITS_PER_DOLLAR
    per_success = str(settled / succeeded) if succeeded else None
    partial = bool(unlinked or unsettled or ambiguous or count_conflicts)
    result = {"successful_turns": succeeded, "failed_turns": len(turns) - succeeded,
              "settled_qwen_usd": str(settled), "unsettled_calls": unsettled,
              "turns_without_receipts": unlinked, "confirmed_no_provider_turns": confirmed_no_calls,
              "possibly_unlinked_requests": ambiguous, "receipt_count_mismatches": count_conflicts,
              "usd_per_successful_turn": None if partial else per_success}
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
        # Input arrival is a lower bound even when the operator queued it during an earlier turn.
        # A later steering message by itself cannot establish when this turn began.
        inbox_fields = {row[1] for row in db.execute("PRAGMA table_info(inbox)")}
        first_input = ("""(SELECT MIN(julianday(input.at)) FROM inbox i JOIN events input ON input.id=i.event_id
                           WHERE i.batch_id=b.id AND i.steered=0)"""
                       if {"batch_id", "event_id", "steered"} <= inbox_fields else "NULL")
        # A tool-only success can use an earlier notification as its outcome. The next batch's
        # outcome is then a safe upper bound because a room has only one active batch at a time.
        ordinary = ("NOT EXISTS (SELECT 1 FROM notifications n WHERE n.event_id=b.outcome)"
                    if "notifications" in tables else "b.status='failed'")
        last_possible = f"""CASE WHEN {ordinary} THEN julianday(e.at) ELSE
                (SELECT julianday(next_event.at) FROM batches next JOIN events next_event ON next_event.id=next.outcome
                 WHERE next.id>b.id ORDER BY next.id LIMIT 1) END"""
        for batch_id, status, first, last in db.execute(f"""SELECT b.id,b.status,{first_input},{last_possible}
                FROM batches b JOIN events e ON e.id=b.outcome
                WHERE b.status IN ('complete','failed') AND date(e.at) BETWEEN ? AND ?
                AND (? IS NULL OR b.id=?)""", (start_day, end_day, batch, batch)):
            batches[batch_id] = status, first, last
        receipts = defaultdict(list)
        possible = defaultdict(set)
        verified_counts = {}
        if batches:
            if "room_verified_request_counts" in billing:
                verified_counts = dict(db.execute("""SELECT batch_id,request_count FROM billing.room_verified_request_counts
                        WHERE thread=? AND batch_id BETWEEN ? AND ?""", (room[0], min(batches), max(batches))))
            fields, joins = routing_receipt_sql(db, {"billing." + name for name in billing}, "billing.",
                                                only={"route", "provider"})
            rows = db.execute(f"""SELECT d.batch_id,r.actual {fields} FROM billing.request_details d
                LEFT JOIN billing.requests r USING(id) {joins}
                WHERE d.thread=? AND d.batch_id BETWEEN ? AND ?""", (room[0], min(batches), max(batches)))
            for row in rows:
                if row["batch_id"] in batches:
                    receipts[row["batch_id"]].append(dict(row))
            recovered = set()
            if "recovered_room_requests" in billing:
                # A historical link supplements missing identifiers. Any conflicting native
                # identifier wins, and a complete native link already supplied its receipt above.
                for row in db.execute(f"""SELECT h.request_id,h.thread,h.batch_id,r.actual,
                        h.route AS old_route,h.provider AS old_provider {fields}
                        FROM billing.recovered_room_requests h JOIN billing.requests r ON r.id=h.request_id
                        LEFT JOIN billing.request_details d ON d.id=r.id {joins}
                        WHERE (d.thread IS NULL OR d.batch_id IS NULL)
                          AND (d.thread IS NULL OR d.thread=h.thread)
                          AND (d.batch_id IS NULL OR d.batch_id=h.batch_id)"""):
                    recovered.add(row["request_id"])
                    if row["thread"] == room[0] and row["batch_id"] in batches:
                        receipt = dict(row)
                        receipt["route"] = receipt.get("route") or receipt["old_route"]
                        receipt["provider"] = receipt.get("provider") or receipt["old_provider"]
                        receipts[row["batch_id"]].append(receipt)
            # A separately audited total can establish complete coverage, including a turn that
            # failed before admission. Native costs still count if later evidence disagrees.
            fully_linked = {bid for bid, count in verified_counts.items() if len(receipts[bid]) == count}
            # Legacy rows may have only their UTC reservation day. Treat its entire duration
            # as possible unless the recorded admission timestamp agrees with that day.
            stamp = ("CASE WHEN date(d.at)=r.day THEN julianday(d.at) END" if "at" in detail_fields else "NULL")
            orphans = db.execute(f"""SELECT r.id,d.batch_id,julianday(r.day),{stamp} FROM billing.requests r
                LEFT JOIN billing.request_details d USING(id)
                WHERE (d.thread IS NULL AND (d.batch_id IS NULL OR d.batch_id BETWEEN ? AND ?))
                    OR (d.thread=? AND d.batch_id IS NULL)""", (min(batches), max(batches), room[0]))
            for key, known_batch, day, at in orphans:
                if key in recovered or (known_batch is not None and known_batch not in batches):
                    continue
                candidates = batches if known_batch is None else (known_batch,)
                for candidate in candidates:
                    _, first, last = batches[candidate]
                    if candidate not in fully_linked and _could_overlap(day, at, first, last):
                        possible[candidate].add(key)

    all_turns = []
    by_route, by_provider = defaultdict(list), defaultdict(list)
    for batch_id, (status, _, _) in batches.items():
        rows = receipts[batch_id]
        turn = _Turn(status, rows, possible[batch_id], verified_counts.get(batch_id))
        route = _group_label(r.get("route") if r.get("route") in ROUTES else None for r in rows)
        provider = _group_label(safe_provider(r.get("provider")) for r in rows)
        all_turns.append(turn)
        by_route[route].append(turn)
        by_provider[provider, route].append(turn)
    ranked = sorted(by_provider.items(), key=lambda item: (-len(item[1]), item[0]))
    overall = _summary(all_turns)
    return {"overall": overall,
            "grouping": "Route and provider averages cover the turns identified by their linked receipts. "
                        "Turns without receipts are grouped under unknown. A minimum includes only settled charges.",
            "by_route": {route: _summary(turns) for route, turns in by_route.items()},
            "by_provider_and_route": [{"provider": provider, "route": route, **_summary(turns)}
                                      for (provider, route), turns in ranked[:provider_limit]],
            "omitted_provider_turns": sum(len(turns) for _, turns in ranked[provider_limit:]),
            "unattributed_requests_in_period": overall["possibly_unlinked_requests"]}
