"""Direct, metered supervisor requests. Only the operator process holds vendor keys."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from .budget import units, UNITS_PER_DOLLAR
from .luna import MODEL as LUNA_MODEL, MAX_OUTPUT_TOKENS as LUNA_OUTPUT
from .opus import OPUS_MODEL, MAX_OUTPUT_TOKENS as OPUS_OUTPUT


DEFAULT_DAILY_USD = Decimal("5")
MILLION = Decimal(1_000_000)


class VendorError(RuntimeError):
    """Safe to show to the operator. Never contains a request, key, or vendor body."""


class HTTPRejection(VendorError):
    def __init__(self, status):
        super().__init__(f"Supervisor API returned HTTP {status}")
        self.status = status


def request_json(vendor: str, key: str, body: bytes) -> dict:
    if vendor == "luna":
        host, path = "api.openai.com", "/v1/responses"
        headers = {"Authorization": "Bearer " + key}
    else:
        host, path = "api.anthropic.com", "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    connection = http.client.HTTPSConnection(host, timeout=60)
    try:
        connection.request("POST", path, body, {**headers, "Content-Type": "application/json"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise HTTPRejection(response.status)
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise VendorError("Supervisor response exceeded its size limit")
            return json.loads(data)
    finally:
        connection.close()


def _tokens(obj, field, *, optional=False):
    value = obj.get(field, 0 if optional else None)
    if type(value) is not int or value < 0:
        raise VendorError("Incomplete supervisor usage")
    return value


def usage_cost(vendor: str, usage: dict):
    inputs, outputs = _tokens(usage, "input_tokens"), _tokens(usage, "output_tokens")
    if vendor == "opus":
        # No Anthropic cache_control blocks in the trusted request. Account for
        # cache usage separately if the API nevertheless returns any.
        reads = _tokens(usage, "cache_read_input_tokens", optional=True)
        writes = _tokens(usage, "cache_creation_input_tokens", optional=True)
        cost = (inputs * 15 + outputs * 75 + reads * Decimal("1.5") + writes * 30) / MILLION
        return inputs + reads + writes, outputs, reads, writes, cost
    details = usage.get("input_tokens_details") or {}
    reads = _tokens(details, "cached_tokens", optional=True)
    writes = _tokens(details, "cache_write_tokens", optional=True)
    if reads + writes > inputs:
        raise VendorError("Inconsistent supervisor cache usage")
    input_cost = (inputs - reads - writes) * Decimal(".10") + reads * Decimal(".01") + writes * Decimal(".125")
    if inputs > 272_000:
        cost = input_cost * 2 + outputs * Decimal(".75")
    else:
        cost = input_cost + outputs * Decimal(".50")
    return inputs, outputs, reads, writes, cost / MILLION


class ReviewerVendors:
    def __init__(self, state: Path, limit: Decimal):
        if not limit.is_finite() or limit < 0 or limit > 1_000_000:
            raise ValueError("Invalid daily supervisor allowance")
        self.limit = limit
        self.path = state / "review-costs.sqlite"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, vendor TEXT NOT NULL,
                purpose TEXT NOT NULL, reserved INTEGER NOT NULL, cost INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, cache_reads INTEGER, cache_writes INTEGER,
                ms INTEGER, status TEXT NOT NULL)""")

    def call(self, vendor, payload, *, purpose):
        if vendor == "luna" and purpose in ("tool", "spot") and payload.get("model") == LUNA_MODEL:
            env, output_limit = "OPENAI_API_KEY", LUNA_OUTPUT
            output = payload.get("max_output_tokens")
        elif vendor == "opus" and purpose in ("consult", "escalation") and payload.get("model") == OPUS_MODEL:
            env, output_limit = "ANTHROPIC_API_KEY", OPUS_OUTPUT
            output = payload.get("max_tokens")
        else:
            raise VendorError("Invalid supervisor request")
        key = os.environ.get(env)
        if not key:
            raise VendorError(f"Missing {env} for required review")
        if type(output) is not int or not 0 < output <= output_limit:
            raise VendorError("Invalid supervisor output limit")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        # One token per serialized byte plus generous framing overhead, limited by
        # the vendor model window. Only trusted, text-only requests are admitted.
        byte_bound = len(body) + 4096
        if vendor == "luna":
            reserve = (min(1_050_000, byte_bound) * Decimal(".25") + output * Decimal(".75")) / MILLION
        else:
            reserve = (min(200_000, byte_bound) * 15 + output * 75) / MILLION
        request_id = uuid4().hex
        held = units(reserve)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            day = now.date().isoformat()
            used, = db.execute("SELECT COALESCE(SUM(COALESCE(cost,reserved)),0) FROM calls WHERE day=?",
                               (day,)).fetchone()
            if used + held > units(self.limit):
                raise VendorError("Insufficient unreserved supervisor allowance")
            db.execute("INSERT INTO calls(id,at,day,vendor,purpose,reserved,status) VALUES(?,?,?,?,?,?,?)",
                       (request_id, now.isoformat(), day, vendor, purpose, held, "pending"))
        started = time.monotonic()
        status, charge = "ambiguous", None
        tokens = (None, None, None, None)
        try:
            result = request_json(vendor, key, body)
            observed = result.get("model", "")
            if vendor == "luna":
                if not re.fullmatch(re.escape(LUNA_MODEL) + r"(?:-\d{4}-\d\d-\d\d)?", observed):
                    raise VendorError("Unexpected Luna model identity")
                if result.get("service_tier", "default") != "default":
                    raise VendorError("Unexpected Luna service tier")
            elif observed != OPUS_MODEL:
                raise VendorError("Unexpected Opus model identity")
            ins, outs, reads, writes, cost = usage_cost(vendor, result["usage"])
            tokens, charge, status = (ins, outs, reads, writes), units(cost), "metered"
            return result
        except HTTPRejection as exc:
            status = f"http_{exc.status}"
            if exc.status in (400, 401, 403, 404, 413, 422, 429):
                charge = 0
            raise
        except (OSError, http.client.HTTPException, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise VendorError("Supervisor request or usage could not be confirmed") from exc
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("""UPDATE calls SET cost=?, input_tokens=?, output_tokens=?, cache_reads=?,
                    cache_writes=?, ms=?, status=? WHERE id=?""", (charge, *tokens, elapsed, status, request_id))

    def today(self):
        day = datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT vendor, purpose, COUNT(*) AS calls, COALESCE(SUM(cost),0) AS cost,
                COALESCE(SUM(CASE WHEN cost IS NULL THEN reserved ELSE 0 END),0) AS held,
                COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens,
                COALESCE(SUM(cache_reads),0) AS cached_read_tokens, COALESCE(SUM(cache_writes),0) AS cache_write_tokens,
                ROUND(AVG(ms)) AS mean_latency_ms
                FROM calls WHERE day=? GROUP BY vendor,purpose ORDER BY vendor,purpose""", (day,)).fetchall()
        breakdown = []
        for row in rows:
            entry = dict(row)
            entry["estimated_usd"] = str(Decimal(entry.pop("cost")) / UNITS_PER_DOLLAR)
            entry["reserved_usd"] = str(Decimal(entry.pop("held")) / UNITS_PER_DOLLAR)
            breakdown.append(entry)
        return {
            "utc_day": day, "limit_usd": str(self.limit),
            "estimated_spend_usd": str(Decimal(sum(row["cost"] for row in rows)) / UNITS_PER_DOLLAR),
            "reserved_usd": str(Decimal(sum(row["held"] for row in rows)) / UNITS_PER_DOLLAR),
            "by_purpose": breakdown,
        }
