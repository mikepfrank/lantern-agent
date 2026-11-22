"""Operator reporting from the two daily ledgers, including pre-attribution records."""

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
import sqlite3
from statistics import median

from .budget import REQUEST_OUTCOMES, TIMING_FIELDS, UNITS_PER_DOLLAR, reported_usd, routing_receipt_sql
from .luna import MODEL as LUNA_MODEL
from .opus import OPUS_MODEL
from .routing import ROUTES, safe_provider
from .transport import MODEL as QWEN_MODEL
from .turn_metrics import room_turn_metrics


MODELS = {"qwen": QWEN_MODEL, "luna": LUNA_MODEL, "opus": OPUS_MODEL}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_reads", "cache_writes", "reasoning_tokens")
PROVIDER_GROUP_LIMIT = 8
FAILED_REQUEST_OUTCOMES = ("empty_output", "timeout", "interrupted", "failed", "incomplete")
METRIC_NOTES = ("TTFT is the interval from upstream dispatch to the first observed nonempty generated delta, "
                "including reasoning and tool arguments. Timing and speed samples use completed calls. "
                "Estimated decoding tokens/s pools output tokens minus one per call over time from first delta "
                "to end. Streaming chunks can contain multiple tokens. Effective tokens/s includes the wait "
                "from dispatch. Cost/token pools the API-reported USD charge and reported input plus output "
                "tokens on the same calls, including cached or reasoning tokens already counted there. "
                "Provider labels are untrusted OpenRouter metadata. A routing preference can select different "
                "providers on different calls. completed_calls counts API completions. failed_calls includes "
                "empty outputs, timeouts, interruptions, incomplete responses and other observed request failures. "
                "timeout_calls is a subset. request_outcomes counts each request once; older unclassified calls "
                "are unknown, and pending calls are still open. Usable output means text or a structured call, "
                "without judging whether the requested task was fulfilled. request_failures_by_provider covers "
                "all providers with measured failures, including those outside the timing table; a null provider "
                "means the endpoint is unknown. room_turns uses the UTC date of each recorded room outcome. "
                "Cost per successful turn includes Qwen spend for both complete and failed turns.")


def _read_period(path, start_day, end_day, *, reviews):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if reviews:
            records = [dict(row) for row in db.execute("SELECT * FROM calls WHERE day BETWEEN ? AND ?", (start_day, end_day))]
        else:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            sql = """SELECT r.id, r.reserved, r.actual AS cost FROM requests r WHERE r.day BETWEEN ? AND ?"""
            if "request_details" in tables:
                fields, join = routing_receipt_sql(db, tables)
                sql = f"""SELECT r.id, r.reserved, r.actual AS cost, d.at, d.thread AS thread_id, d.batch_id,
                                d.trigger, d.status, d.input_tokens, d.output_tokens, d.cache_reads,
                                d.cache_writes, d.reasoning_tokens, d.ms, d.charge_source {fields}
                         FROM requests r LEFT JOIN request_details d ON r.id=d.id {join} WHERE r.day BETWEEN ? AND ?"""
            records = [dict(row) for row in db.execute(sql, (start_day, end_day))]
    for record in records:
        if reviews:
            record["charge_source"] = "token_estimate" if record["status"] == "metered" else None
        else:
            record["vendor"] = "qwen"
            record["provider"] = safe_provider(record.get("provider"))
            if record.get("request_outcome") not in REQUEST_OUTCOMES:
                record["request_outcome"] = None
            record["reported_cost_usd"] = reported_usd(record.get("reported_cost_usd"))
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
    detail = {
        "id": r["id"], "at_utc": r.get("at"), "model": r["vendor"], "purpose": r.get("purpose"),
        "thread_id": r.get("thread_id"), "batch_id": r.get("batch_id"), "trigger": r.get("trigger"),
        "tool": r.get("tool"), "status": r["status"], **{f: r.get(f) for f in TOKEN_FIELDS},
        "latency_ms": r.get("ms"), "charge_source": r.get("charge_source"),
        "settled_spend_usd": _usd(r["cost"]) if r["cost"] is not None else None,
        "held_usd": _usd(r["reserved"] if r["cost"] is None else 0),
    }
    if r["vendor"] == "qwen":
        route = r.get("route")
        detail["route"] = route if route in ROUTES else None
        for key in ("dispatch_at", *TIMING_FIELDS, "provider", "reported_cost_usd", "request_outcome"):
            detail[key] = r.get(key)
    return detail


def _request_reliability(rows):
    outcomes = Counter(r.get("request_outcome") or ("pending" if r["status"] == "pending" else "unknown")
                       for r in rows)
    failures = sum(outcomes[k] for k in FAILED_REQUEST_OUTCOMES)
    return {"failed_calls": failures, "timeout_calls": outcomes["timeout"], "request_outcomes": dict(outcomes)}


def _route_summary(rows):
    completed = [r for r in rows if r["status"] == "completed"]
    timings = {}
    for field in TIMING_FIELDS:
        values = [r[field] for r in completed if r.get(field) is not None]
        timings[field] = {"samples": len(values), "mean_ms": round(sum(values) / len(values), 1) if values else None,
                          "median_ms": median(values) if values else None}
    # Effective output rate includes initial processing and any reasoning.
    # Pool tokens and elapsed time over the same completed requests.
    sampled = [r for r in completed if r.get("output_tokens") is not None and (r.get("response_end_ms") or 0) > 0]
    tokens = sum(r["output_tokens"] for r in sampled)
    elapsed_ms = sum(r["response_end_ms"] for r in sampled)
    costs = [Decimal(r["reported_cost_usd"]) for r in rows if r.get("reported_cost_usd") is not None]
    decoding = [r for r in sampled if r.get("first_token_ms") is not None
                and r["first_token_ms"] < r["response_end_ms"] and r["output_tokens"] > 1]
    decoded_tokens = sum(r["output_tokens"] - 1 for r in decoding)
    decoding_ms = sum(r["response_end_ms"] - r["first_token_ms"] for r in decoding)
    priced = [r for r in rows if r.get("reported_cost_usd") is not None and r.get("input_tokens") is not None
              and r.get("output_tokens") is not None and r["input_tokens"] + r["output_tokens"] > 0]
    priced_input = sum(r["input_tokens"] for r in priced)
    priced_output = sum(r["output_tokens"] for r in priced)
    priced_tokens = priced_input + priced_output
    cache_reads = [r["cache_reads"] for r in priced if r.get("cache_reads") is not None]
    priced_cost = sum(Decimal(r["reported_cost_usd"]) for r in priced)
    return {"calls": len(rows), "completed_calls": len(completed), **_request_reliability(rows),
            "reported_cost_calls": len(costs),
            "reported_cost_usd": str(sum(costs)) if costs else None, "completed_timings": timings,
            "decoding_samples": len(decoding),
            "decoding_output_tokens_per_second": round(decoded_tokens * 1000 / decoding_ms, 2) if decoding_ms else None,
            "priced_token_samples": len(priced), "priced_tokens": priced_tokens,
            "priced_input_tokens": priced_input, "priced_output_tokens": priced_output,
            "priced_cached_input_tokens": sum(cache_reads) if cache_reads else None,
            "priced_cache_read_samples": len(cache_reads),
            "calls_without_paired_cost_and_tokens": len(rows) - len(priced),
            "usd_per_token": str(priced_cost / priced_tokens) if priced_tokens else None,
            "usd_per_million_tokens": str(priced_cost * 1_000_000 / priced_tokens) if priced_tokens else None,
            "throughput_samples": len(sampled),
            "effective_output_tokens_per_second": round(tokens * 1000 / elapsed_ms, 2) if elapsed_ms else None}


def _provider_groups(rows):
    by_route = {route: [] for route in ROUTES}
    by_provider = defaultdict(list)
    failures = defaultdict(Counter)
    missing_route = missing_provider = 0
    for row in rows:
        route, provider = row.get("route"), row.get("provider")
        if row.get("request_outcome") in FAILED_REQUEST_OUTCOMES:
            failures[provider][row["request_outcome"]] += 1
        if route not in by_route:
            missing_route += 1
            continue
        by_route[route].append(row)
        if provider is None:
            missing_provider += 1
        else:
            by_provider[(provider, route)].append(row)
    ranked = sorted(by_provider.items(), key=lambda item: (-len(item[1]), item[0]))
    shown = [{"provider": provider, "route": route, **_route_summary(group)}
             for (provider, route), group in ranked[:PROVIDER_GROUP_LIMIT]]
    omitted = ranked[PROVIDER_GROUP_LIMIT:]
    failure_groups = [{"provider": provider, "failed_calls": counts.total(), "timeout_calls": counts["timeout"],
                       "failure_types": dict(counts)}
                      for provider, counts in sorted(failures.items(), key=lambda item: (-item[1].total(), item[0] or ""))]
    return {"qwen_by_route": {route: _route_summary(group) for route, group in by_route.items()},
            "request_failures_by_provider": failure_groups,
            "qwen_by_provider_and_route": shown, "qwen_calls_without_route": missing_route,
            "routed_calls_without_provider": missing_provider, "omitted_provider_groups": len(omitted),
            "omitted_provider_calls": sum(len(group) for _, group in omitted), "metric_notes": METRIC_NOTES}


def _utc_day(day):
    try:
        if date.fromisoformat(day).isoformat() == day:
            return day
    except (ValueError, TypeError):
        pass
    raise ValueError("Use a UTC day in YYYY-MM-DD format")


def provider_summary(state: Path, day: str | None = None) -> dict:
    """Read only Qwen receipts for one UTC day or the last seven UTC dates."""
    if day is None:
        end = datetime.now(timezone.utc).date()
        start_day, end_day = (end - timedelta(days=6)).isoformat(), end.isoformat()
    else:
        start_day = end_day = _utc_day(day)
    qwen_rows = _read_period(state / "usage.sqlite", start_day, end_day, reviews=False)
    return {"start_day": start_day, "end_day": end_day, **_provider_groups(qwen_rows),
            "room_turns": room_turn_metrics(state, start_day, end_day)}


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
    day = now.date().isoformat() if day is None else _utc_day(day)
    if batch is not None and (type(batch) is not int or batch < 1):
        raise ValueError("Use a positive batch ID")
    daily = _read_period(state / "usage.sqlite", day, day, reviews=False)
    daily.extend(_read_period(state / "review-costs.sqlite", day, day, reviews=True))
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
    qwen_rows = [r for r in rows if r["vendor"] == "qwen"]
    qwen = sum(r["cost"] for r in qwen_rows if r["cost"] is not None)
    supervisors = sum(r["cost"] for r in rows if r["vendor"] != "qwen" and r["cost"] is not None)
    result = {
        "utc_day": day, "as_of_utc": now.isoformat(), "batch_id": batch,
        "calls_without_batch_id_on_day": unassigned,
        "by_model": by_model, "reviews_by_purpose": by_purpose,
        **_provider_groups(qwen_rows),
        "room_turns": room_turn_metrics(state, day, day, batch=batch),
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
