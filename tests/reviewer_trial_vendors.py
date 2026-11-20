"""Opt-in, metered offline adjudications. Never dispatches a proposed tool."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from lantern.budget import UNITS_PER_DOLLAR, units
from lantern.opus import OPUS_MODEL, OpusResponseError
from lantern.opus_audit import OpusCapture
from lantern.review_vendors import (
    DEFAULT_DAILY_USD, HTTPRejection, MILLION, ReviewerVendors, VendorError, request_json,
)


SONNET = "claude-sonnet-4-6"
GPT = "gpt-5.4"
# Standard direct-API prices per million tokens. Requests are short, text-only,
# routed globally, and never request paid tools, prompt-cache writes, or priority.
PRICES = {OPUS_MODEL: (Decimal(15), Decimal(75)), SONNET: (Decimal(3), Decimal(15)),
          GPT: (Decimal("2.5"), Decimal(15))}
MAX_TRIAL_USD = Decimal("0.95")


def _count(data, key, default=None):
    value = data.get(key, default)
    if type(value) is not int or value < 0:
        raise ValueError("invalid_usage")
    return value


def _cost(model, usage):
    base, output_rate = PRICES[model]
    inputs = _count(usage, "input_tokens")
    outputs = _count(usage, "output_tokens")
    reasoning = None
    if model == GPT:
        details = usage.get("input_tokens_details") or {}
        reads = _count(details, "cached_tokens", 0)
        writes = _count(details, "cache_write_tokens", 0)
        reasoning = _count(usage.get("output_tokens_details") or {}, "reasoning_tokens", 0)
        if reads + writes > inputs or reasoning > outputs:
            raise ValueError("invalid_usage")
        # Reported input and output already include cache hits and reasoning.
        input_cost = (inputs - reads) * base + reads * base / 10
    else:
        reads = _count(usage, "cache_read_input_tokens", 0)
        writes = _count(usage, "cache_creation_input_tokens", 0)
        split = usage.get("cache_creation") or {}
        long_writes = _count(split, "ephemeral_1h_input_tokens", 0)
        short_writes = _count(split, "ephemeral_5m_input_tokens", writes - long_writes)
        if short_writes + long_writes != writes:
            raise ValueError("invalid_usage")
        input_cost = (inputs + Decimal("0.1") * reads + Decimal("1.25") * short_writes + 2 * long_writes) * base
        inputs += reads + writes
    return {"input_tokens": inputs, "output_tokens": outputs, "cache_reads": reads, "cache_writes": writes,
            "reasoning_tokens": reasoning}, units((input_cost + outputs * output_rate) / MILLION)


def _get_text(model, response):
    if model == GPT:
        if response.get("status") != "completed":
            return None, "incomplete_output"
        blocks = [part for item in response.get("output", []) if item.get("type") == "message"
                  for part in item.get("content", [])]
        kind = "output_text"
    else:
        if response.get("stop_reason") != "end_turn":
            return None, "incomplete_output"
        blocks = response.get("content", [])
        kind = "text"
    if len(blocks) != 1 or blocks[0].get("type") != kind or not isinstance(blocks[0].get("text"), str):
        return None, "invalid_output"
    return blocks[0]["text"], "ok"


class TrialVendors:
    def __init__(self, state: Path, thread: str, cap_usd=MAX_TRIAL_USD):
        if not re.fullmatch(r"reviewer-trial-[a-z0-9-]{1,81}", thread):
            raise ValueError("Use a dedicated reviewer-trial- identifier")
        self.thread = thread
        cap = Decimal(cap_usd)
        if not cap.is_finite() or not 0 < cap <= MAX_TRIAL_USD:
            raise ValueError("Invalid experiment cap")
        self.cap = units(cap)
        if not (state / "review-costs.sqlite").is_file():
            raise ValueError("Select the existing shared supervisor ledger")
        daily_limit = Decimal(os.environ.get("LANTERN_REVIEW_DAILY_USD", str(DEFAULT_DAILY_USD)))
        self.ledger = ReviewerVendors(state, daily_limit)

    def _reserve(self, request_id, label, model, held, capture):
        with closing(sqlite3.connect(self.ledger.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            day = now.date().isoformat()
            if db.execute("SELECT 1 FROM calls WHERE id=?", (request_id,)).fetchone():
                return "already_attempted"
            spent = "SELECT COALESCE(SUM(COALESCE(cost,reserved)),0) FROM calls WHERE "
            if db.execute(spent + "thread_id=?", (self.thread,)).fetchone()[0] + held > self.cap:
                return "trial_budget"
            if db.execute(spent + "day=?", (day,)).fetchone()[0] + held > units(self.ledger._limit_for_day(db, day)):
                return "daily_budget"
            db.execute("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,status,thread_id,tool_call_id)
                          VALUES(?,?,?,?,'reviewer_trial',?,'pending',?,?)""",
                       (request_id, now.isoformat(), day, model, held, self.thread, label))
            if capture:
                db.execute("INSERT INTO opus_responses(request_id,capture_status) VALUES(?, 'pending')", (request_id,))
        return None

    def run(self, model, system, case_text, label, *, max_output_tokens=None, verdict_schema=None):
        if model not in PRICES or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", label):
            raise ValueError("Unknown trial model or invalid case label")
        output = 768 if max_output_tokens is None else max_output_tokens
        if type(output) is not int or not 16 <= output <= 1536:
            raise ValueError("Invalid output token cap")
        if not all(isinstance(t, str) and t.strip() for t in (system, case_text)):
            raise ValueError("Trial system and case must be supplied as text")
        if verdict_schema is not None and model != SONNET:
            raise ValueError("The structured-output follow-up only enrolls Sonnet")
        identifier = "\0".join(("reviewer-trial-v1", self.thread, model, label))
        request_id = hashlib.sha256(identifier.encode()).hexdigest()[:32]
        record = {"requested_model": model, "request_id": request_id, "model": None, "text": None,
                  "cost_usd": None, "held_usd": "0", "ms": None, "ledger_status": None}
        key = os.environ.get("OPENAI_API_KEY" if model == GPT else "ANTHROPIC_API_KEY")
        if not key:
            return {**record, "status": "missing_key"}

        if model == GPT:
            payload = {"model": model, "instructions": system, "input": case_text, "store": False,
                       "reasoning": {"effort": "low"}, "service_tier": "default", "max_output_tokens": output}
        else:
            payload = {"model": model, "system": system, "messages": [{"role": "user", "content": case_text}],
                       "max_tokens": output}
            if verdict_schema is not None:
                payload["output_config"] = {"format": {"type": "json_schema", "schema": verdict_schema}}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        if len(body) > 64 * 1024:
            raise ValueError("Trial case exceeds the text input limit")
        in_rate, out_rate = PRICES[model]
        # No cache-control blocks. The conservative byte bound includes framing.
        held = units(((len(body) + 4096) * in_rate + output * out_rate) / MILLION)
        capture = OpusCapture(self.ledger.path, request_id) if model != GPT else None
        if capture:
            capture.prepare_directory()
        blocked = self._reserve(request_id, label, model, held, capture)
        if blocked:
            return {**record, "status": blocked}

        started = time.monotonic()
        charge = None
        ledger_status = "ambiguous"
        status = "transport_error"
        try:
            response = request_json("luna" if model == GPT else "opus", key, body, audit=capture)
            observed = response.get("model")
            record["model"] = observed
            if observed != model and not (model == GPT and observed == "gpt-5.4-2026-03-05"):
                status = "unexpected_model"
            elif model == GPT and response.get("service_tier", "default") != "default":
                status = "unexpected_service_tier"
            else:
                counters, charge = _cost(model, response["usage"])
                record.update(counters)
                ledger_status = "metered"
                record["text"], status = _get_text(model, response)
        except HTTPRejection as exc:
            status = ledger_status = f"http_{exc.status}"
            if exc.status in (400, 401, 403, 404, 413, 422, 429):
                charge = 0
        except (KeyError, ValueError, TypeError, AttributeError, OSError, http.client.HTTPException,
                VendorError, OpusResponseError) as exc:
            status = "response_error_" + type(exc).__name__
        finally:
            ms = round((time.monotonic() - started) * 1000)
            with closing(sqlite3.connect(self.ledger.path, timeout=10)) as db, db:
                if capture:
                    capture.save(db)
                db.execute("""UPDATE calls SET cost=?,input_tokens=?,output_tokens=?,cache_reads=?,cache_writes=?,
                              ms=?,status=? WHERE id=?""",
                           (charge, record.get("input_tokens"), record.get("output_tokens"), record.get("cache_reads"),
                            record.get("cache_writes"), ms, ledger_status, request_id))
            record.update(ms=ms, ledger_status=ledger_status,
                          held_usd=str(Decimal(held) / UNITS_PER_DOLLAR) if charge is None else "0",
                          cost_usd=str(Decimal(charge) / UNITS_PER_DOLLAR) if charge is not None else None)
        return {**record, "status": status}
