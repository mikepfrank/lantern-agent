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
from .opus import AuditedReply, OpusResponseError, OPUS_MODEL, MAX_OUTPUT_TOKENS as OPUS_OUTPUT
from .opus_audit import OpusCapture


DEFAULT_DAILY_USD = Decimal("5")
MILLION = Decimal(1_000_000)


class VendorError(RuntimeError):
    """Safe to show to the operator. Never contains a request, key, or vendor body."""


class OpusTransportError(VendorError):
    """No complete Opus response could be received."""


class ReviewBudgetUnavailable(VendorError):
    """The supervisor reservation would exceed the named UTC day's allowance."""

    def __init__(self, day: str):
        super().__init__("Insufficient unreserved supervisor allowance")
        self.day = day


class HTTPRejection(VendorError):
    def __init__(self, status):
        super().__init__(f"Supervisor API returned HTTP {status}")
        self.status = status


def request_json(vendor: str, key: str, body: bytes, *, audit: OpusCapture | None = None) -> dict:
    if vendor == "luna":
        host, path = "api.openai.com", "/v1/responses"
        headers = {"Authorization": "Bearer " + key}
    else:
        if audit is None:
            raise VendorError("Opus response capture is required")
        host, path = "api.anthropic.com", "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    connection = http.client.HTTPSConnection(host, timeout=60)
    try:
        connection.request("POST", path, body, {**headers, "Content-Type": "application/json"})
        with connection.getresponse() as response:
            if audit is not None:
                audit.capture(response)
            if response.status != 200:
                raise HTTPRejection(response.status)
            if audit is not None:
                if audit.status != "complete":
                    raise OpusTransportError("Incomplete Opus response")
                if audit.body_bytes > 2_000_000:
                    raise OpusResponseError("response_too_large")
                try:
                    data = audit.path.read_bytes()
                except OSError:
                    audit.failure = "archive_error"
                    raise VendorError("Opus archive could not be read") from None
                try:
                    result = json.loads(data)
                except (ValueError, RecursionError):
                    raise OpusResponseError("invalid_json") from None
                if not isinstance(result, dict):
                    raise OpusResponseError("invalid_json")
                return result
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
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS review_daily_grants (
                day TEXT PRIMARY KEY NOT NULL, extra_units INTEGER NOT NULL
                CHECK(typeof(extra_units)='integer' AND extra_units>=0))""")
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, vendor TEXT NOT NULL,
                purpose TEXT NOT NULL, reserved INTEGER NOT NULL, cost INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, cache_reads INTEGER, cache_writes INTEGER,
                ms INTEGER, status TEXT NOT NULL, thread_id TEXT, batch_id INTEGER, tool TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
            for name, sql_type in (("thread_id", "TEXT"), ("batch_id", "INTEGER"), ("tool", "TEXT"),
                                   ("tool_call_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE calls ADD COLUMN {name} {sql_type}")
            db.execute("""CREATE TABLE IF NOT EXISTS opus_responses(
                request_id TEXT PRIMARY KEY REFERENCES calls(id), http_status INTEGER,
                capture_status TEXT NOT NULL, body_bytes INTEGER, sha256 TEXT,
                failure_code TEXT, validation TEXT)""")

    def _limit_for_day(self, db, day):
        grant = db.execute("SELECT extra_units FROM review_daily_grants WHERE day=?", (day,)).fetchone()
        return self.limit + Decimal(grant[0]) / UNITS_PER_DOLLAR if grant else self.limit

    def call(self, vendor, payload, *, purpose, thread_id=None, batch_id=None, tool=None, tool_call_id=None):
        """Attribute each paid call to the host's active thread, batch and reviewed tool."""
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
        # Encoded byte counts bound text tokenization. For images, a small
        # compressed file can represent many vision tokens. Reserve against the
        # whole model window and settle from the provider's actual usage.
        byte_bound = len(body) + 4096
        if vendor == "luna":
            vision = any(isinstance(item.get("content"), list) and
                         any(part.get("type") == "input_image" for part in item["content"])
                         for item in payload.get("input", ()))
            input_bound = 1_050_000 if vision else min(1_050_000, byte_bound)
            reserve = (input_bound * Decimal(".25") + output * Decimal(".75")) / MILLION
        else:
            reserve = (min(200_000, byte_bound) * 15 + output * 75) / MILLION
        request_id = uuid4().hex
        audit = OpusCapture(self.path, request_id) if vendor == "opus" else None
        held = units(reserve)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            day = now.date().isoformat()
            used, = db.execute("SELECT COALESCE(SUM(COALESCE(cost,reserved)),0) FROM calls WHERE day=?",
                               (day,)).fetchone()
            if used + held > units(self._limit_for_day(db, day)):
                raise ReviewBudgetUnavailable(day)
            db.execute("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,status,thread_id,batch_id,tool,tool_call_id)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (request_id, now.isoformat(), day, vendor, purpose, held, "pending", thread_id, batch_id,
                        tool, tool_call_id))
            if audit is not None:
                db.execute("INSERT INTO opus_responses(request_id,capture_status) VALUES(?, 'pending')",
                           (request_id,))
        started = time.monotonic()
        status, charge = "ambiguous", None
        tokens = (None, None, None, None)
        try:
            if audit is not None:
                try:
                    audit.prepare_directory()
                except OSError:
                    status, charge = "archive_error", 0  # No request was sent.
                    audit.status, audit.failure = "archive_error", "archive_error"
                    raise VendorError("Opus archive is unavailable") from None
                result = request_json(vendor, key, body, audit=audit)
            else:
                result = request_json(vendor, key, body)
            observed = result.get("model", "")
            if vendor == "luna":
                if not re.fullmatch(re.escape(LUNA_MODEL) + r"(?:-\d{4}-\d\d-\d\d)?", observed):
                    raise VendorError("Unexpected Luna model identity")
                if result.get("service_tier", "default") != "default":
                    raise VendorError("Unexpected Luna service tier")
            elif observed != OPUS_MODEL:
                raise OpusResponseError("unexpected_model")
            try:
                ins, outs, reads, writes, cost = usage_cost(vendor, result["usage"])
            except (VendorError, KeyError, AttributeError, TypeError):
                if audit is not None:
                    raise OpusResponseError("invalid_usage") from None
                raise
            tokens, charge, status = (ins, outs, reads, writes), units(cost), "metered"
            return AuditedReply(result, audit.record_validation) if audit is not None else result
        except OpusResponseError as exc:
            audit.failure = exc.code
            raise
        except HTTPRejection as exc:
            status = f"http_{exc.status}"
            if audit is not None and audit.failure is None:
                audit.failure = status
            if exc.status in (400, 401, 403, 404, 413, 422, 429):
                charge = 0
            raise
        except (OSError, http.client.HTTPException) as exc:
            if audit is not None:
                if audit.failure == "archive_error":
                    raise VendorError("Opus reply could not be archived") from None
                audit.failure = audit.failure or "transport_error"
                raise OpusTransportError("Opus provider connection failed") from None
            raise VendorError("Supervisor request or usage could not be confirmed") from exc
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise VendorError("Supervisor request or usage could not be confirmed") from exc
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            with closing(sqlite3.connect(self.path)) as db, db:
                if audit is not None:
                    audit.save(db)
                db.execute("""UPDATE calls SET cost=?, input_tokens=?, output_tokens=?, cache_reads=?,
                    cache_writes=?, ms=?, status=? WHERE id=?""", (charge, *tokens, elapsed, status, request_id))

    def today(self, *, day: str | None = None):
        day = day or datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN")
            db.row_factory = sqlite3.Row
            effective_limit = self._limit_for_day(db, day)
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
            "utc_day": day, "limit_usd": str(effective_limit),
            "estimated_spend_usd": str(Decimal(sum(row["cost"] for row in rows)) / UNITS_PER_DOLLAR),
            "reserved_usd": str(Decimal(sum(row["held"] for row in rows)) / UNITS_PER_DOLLAR),
            "by_purpose": breakdown,
        }
