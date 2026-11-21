"""Operator reporting from the two daily ledgers, including pre-attribution records."""

from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3

from .budget import UNITS_PER_DOLLAR
from .luna import MODEL as LUNA_MODEL
from .opus import OPUS_MODEL
from .transport import MODEL as QWEN_MODEL


MODELS = {"qwen": QWEN_MODEL, "luna": LUNA_MODEL, "opus": OPUS_MODEL}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_reads", "cache_writes", "reasoning_tokens")


def _read_day(path, day, *, reviews):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if reviews:
            records = [dict(row) for row in db.execute("SELECT * FROM calls WHERE day=?", (day,))]
        else:
            modern = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='request_details'").fetchone()
            sql = """SELECT r.id, r.reserved, r.actual AS cost FROM requests r WHERE r.day=?"""
            if modern:
                sql = """SELECT r.id, r.reserved, r.actual AS cost, d.at, d.thread AS thread_id, d.batch_id,
                                d.trigger, d.status, d.input_tokens, d.output_tokens, d.cache_reads,
                                d.cache_writes, d.reasoning_tokens, d.ms, d.charge_source
                         FROM requests r LEFT JOIN request_details d ON r.id=d.id WHERE r.day=?"""
            records = [dict(row) for row in db.execute(sql, (day,))]
    for record in records:
        if reviews:
            record["charge_source"] = "token_estimate" if record["status"] == "metered" else None
        else:
            record["vendor"] = "qwen"
            record["status"] = record.get("status") or ("settled" if record["cost"] is not None else "unsettled")
    return records


def _usd(units):
    return format(Decimal(units) / UNITS_PER_DOLLAR, "f")


def _summarize(rows):
    holds = [r for r in rows if r["cost"] is None]
    summary = {
        "calls": len(rows),
        "settled_spend_usd": _usd(sum(r["cost"] for r in rows if r["cost"] is not None)),
        "unsettled_calls": len(holds),
        "held_usd": _usd(sum(r["reserved"] for r in holds)),
    }
    missing = {}
    for field in TOKEN_FIELDS:
        counts = [r[field] for r in rows if r.get(field) is not None]
        summary[field] = sum(counts) if counts else None
        if len(counts) < len(rows):
            missing[field] = len(rows) - len(counts)
    if missing:
        summary["calls_missing_token_counts"] = missing
    times = [r["ms"] for r in rows if r.get("ms") is not None]
    summary["mean_latency_ms"] = round(sum(times) / len(times)) if times else None
    return summary


def _call_detail(r):
    return {
        "id": r["id"], "at_utc": r.get("at"), "model": r["vendor"], "purpose": r.get("purpose"),
        "thread_id": r.get("thread_id"), "batch_id": r.get("batch_id"), "trigger": r.get("trigger"),
        "tool": r.get("tool"), "status": r["status"], **{f: r.get(f) for f in TOKEN_FIELDS},
        "latency_ms": r.get("ms"), "charge_source": r.get("charge_source"),
        "settled_spend_usd": _usd(r["cost"]) if r["cost"] is not None else None,
        "held_usd": _usd(r["reserved"] if r["cost"] is None else 0),
    }


def _room_review_rates(state, day, daily):
    path = state / "chat.sqlite"
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"room", "operator_reviews"} <= tables:
            return None
        room = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()
        if not room or not room[0]:
            return None
        questions = db.execute("SELECT COUNT(*) FROM operator_reviews WHERE thread=? AND date(asked_at)=?",
                               (room[0], day)).fetchone()[0]
    calls = [r for r in daily if r.get("thread_id") == room[0]]
    screens = [r.get("tool_call_id") for r in calls if r["vendor"] == "luna" and r["purpose"] == "tool"]
    proposals = len({call_id for call_id in screens if call_id})
    missing = sum(call_id is None for call_id in screens)
    opus_calls = sum(r["vendor"] == "opus" and r["purpose"] in ("escalation", "context_review") for r in calls)
    return {
        "screened_proposals_with_id": proposals, "tool_screens_without_id": missing,
        "operator_questions_opened": questions, "opus_review_calls": opus_calls,
        "questions_per_100_proposals": round(100 * questions / proposals, 2) if proposals and not missing else None,
        "opus_review_calls_per_100_proposals": round(100 * opus_calls / proposals, 2) if proposals and not missing else None,
    }


def usage_report(state: Path, day: str | None = None, *, calls: bool = False, batch: int | None = None) -> dict:
    """Report recorded charges on their UTC admission day. Filter only by known batch IDs."""
    now = datetime.now(timezone.utc)
    if day is None:
        day = now.date().isoformat()
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError("Use a UTC day in YYYY-MM-DD format") from exc
    if batch is not None and (type(batch) is not int or batch < 1):
        raise ValueError("Use a positive batch ID")
    daily = _read_day(state / "usage.sqlite", day, reviews=False)
    daily.extend(_read_day(state / "review-costs.sqlite", day, reviews=True))
    rows = daily if batch is None else [r for r in daily if r.get("batch_id") == batch]
    # Trial rows carry their exact API model ID in the vendor field. Keep the
    # established live model keys, even on days with no calls to those models.
    trial_models = sorted({r["vendor"] for r in daily if r.get("purpose") in ("reviewer_trial", "shadow_review")})
    models = MODELS.copy()
    for model in trial_models:
        models.setdefault(model, model)
    unassigned = {model: sum(r["vendor"] == model and r.get("batch_id") is None for r in daily)
                  for model in models}
    by_model = {}
    by_purpose = []
    for model, model_id in models.items():
        matching = [r for r in rows if r["vendor"] == model]
        if batch is None or matching:
            by_model[model] = {"model_id": model_id, **_summarize(matching)}
        if model != "qwen":
            for purpose in sorted({r["purpose"] for r in matching}):
                group = [r for r in matching if r["purpose"] == purpose]
                by_purpose.append({"model": model, "purpose": purpose, **_summarize(group)})
    qwen = sum(r["cost"] for r in rows if r["vendor"] == "qwen" and r["cost"] is not None)
    supervisors = sum(r["cost"] for r in rows if r["vendor"] != "qwen" and r["cost"] is not None)
    result = {
        "utc_day": day, "as_of_utc": now.isoformat(), "batch_id": batch,
        "calls_without_batch_id_on_day": unassigned,
        "by_model": by_model, "reviews_by_purpose": by_purpose,
        "supervisor_to_qwen_settled_cost_ratio": round(supervisors / qwen, 4) if qwen else None,
        "cost_basis": "Qwen uses settled ledger charges. Reviewer costs use estimates from vendor token usage. "
                      "The ratio uses settled amounts only. Token totals sum available counts.",
    }
    if batch is not None:
        result["batch_coverage"] = ("Only calls tagged with this batch ID on this UTC day are included."
                                    if rows else "No calls were tagged with this batch ID on this UTC day.")
    else:
        rates = _room_review_rates(state, day, daily)
        if rates is not None:
            result["room_review_rates"] = rates
    if calls:
        ordered = sorted(rows, key=lambda r: (r.get("at") is None, r.get("at") or "", r["id"]))
        result["calls"] = [_call_detail(r) for r in ordered]
    return result
