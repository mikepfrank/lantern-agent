"""Reserve before dispatch and settle against OpenRouter's reported USD cost."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
import sqlite3
from uuid import uuid4

from .routing import ROUTES, safe_provider


UNITS_PER_DOLLAR = 1_000_000_000
MODEL_CONTEXT_TOKENS = 1_024_000
HANDOFF_REMINDER_TOKENS = 180_000
DAILY_USD = Decimal("10")
TIMING_FIELDS = ("response_start_ms", "first_token_ms", "first_output_ms", "response_end_ms")
REQUEST_OUTCOMES = ("usable", "empty_output", "timeout", "interrupted", "failed", "incomplete", "local", "superseded")


def reported_usd(value):
    """Validate a serialized provider charge before showing it or summing samples."""
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if amount.is_finite() and amount >= 0:
        return str(amount)


def dispatch_utc(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def routing_receipt_sql(db, tables, prefix="", *, only=None):
    """Read the receipt fields that exist in this ledger version without upgrading it."""
    fields, joins = [], []
    for table, alias, names in (
        ("request_routes", "p", ("route",)),
        ("request_metrics", "m", ("dispatch_at", *TIMING_FIELDS, "provider", "reported_cost_usd", "request_outcome")),
    ):
        wanted = [name for name in names if only is None or name in only]
        if not wanted or prefix + table not in tables:
            continue
        columns = {r[1] for r in db.execute(f"PRAGMA {prefix}table_info({table})")}
        selected = [f"{alias}.{name}" for name in wanted if name in columns]
        if selected:
            fields.extend(selected)
            joins.append(f"LEFT JOIN {prefix}{table} {alias} ON {alias}.request_id=d.id")
    return "".join(", " + field for field in fields), " ".join(joins)


class BudgetUnavailable(ValueError):
    """The OpenRouter reservation would exceed the named UTC day's allowance."""

    def __init__(self, day: str):
        super().__init__("OpenRouter daily budget has insufficient unreserved funds")
        self.day = day


def units(amount: Decimal) -> int:
    if not amount.is_finite() or amount < 0:
        raise ValueError("Expected a finite, nonnegative cost")
    return int((amount * UNITS_PER_DOLLAR).to_integral_value(rounding=ROUND_CEILING))


class Budget:
    def __init__(self, path: Path, reservation_usd: Decimal):
        self.path = path
        self.reservation = units(reservation_usd)
        if self.reservation <= 0:
            raise ValueError("A request needs a positive reservation")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, day TEXT NOT NULL, reserved INTEGER NOT NULL,
                actual INTEGER CHECK(actual >= 0))""")
            # Older releases and existing databases use the four-column ledger.
            # Keep their charges intact and leave unobserved historical detail unknown.
            db.execute("""CREATE TABLE IF NOT EXISTS request_details (
                id TEXT PRIMARY KEY REFERENCES requests(id), at TEXT NOT NULL, thread TEXT,
                batch_id INTEGER, trigger TEXT, status TEXT NOT NULL, input_tokens INTEGER,
                output_tokens INTEGER, cache_reads INTEGER, cache_writes INTEGER,
                reasoning_tokens INTEGER, ms INTEGER, charge_source TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS details_by_thread ON request_details(thread)")
            db.execute("""CREATE TABLE IF NOT EXISTS request_routes (
                request_id TEXT PRIMARY KEY REFERENCES request_details(id), route TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS request_metrics (
                request_id TEXT PRIMARY KEY REFERENCES request_details(id), dispatch_at TEXT,
                response_start_ms INTEGER, first_token_ms INTEGER, first_output_ms INTEGER, response_end_ms INTEGER,
                provider TEXT, reported_cost_usd TEXT, request_outcome TEXT)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(request_metrics)")}
            if "first_token_ms" not in columns:
                db.execute("ALTER TABLE request_metrics ADD COLUMN first_token_ms INTEGER")
            if "request_outcome" not in columns:
                db.execute("ALTER TABLE request_metrics ADD COLUMN request_outcome TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS handoff_events (
                request_id TEXT PRIMARY KEY REFERENCES request_details(id), event TEXT NOT NULL)""")

    def handoff_reminder_due(self, thread: str | None) -> bool:
        """Use this thread's reported context sizes, even across UTC days and worker restarts."""
        if not thread:
            return False
        with closing(sqlite3.connect(self.path)) as db:
            # Admission order is stable even when a superseded stream settles late.
            rows = db.execute("""SELECT d.input_tokens + COALESCE(d.output_tokens, 0), h.event
                                 FROM request_details AS d
                                 LEFT JOIN handoff_events AS h ON h.request_id=d.id
                                 WHERE d.thread=? AND d.status IN ('completed','incomplete')
                                 ORDER BY d.rowid DESC""", (thread,))
            reached_threshold = False
            for tokens, event in rows:
                if event == "tool_free" or (tokens is not None and tokens < HANDOFF_REMINDER_TOKENS):
                    break
                if event == "reminded":
                    return False
                if tokens is not None:
                    reached_threshold = True
            return reached_threshold

    def reserve(self, payload: dict, *, thread=None, batch_id=None, trigger=None) -> str:
        # Independent of token estimates: reserve for the model's full context
        # at the per-token price ceilings applied by the gateway.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            day = now.date().isoformat()
            used, = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual, reserved)), 0) FROM requests WHERE day=?",
                (day,),
            ).fetchone()
            if used + self.reservation > units(DAILY_USD):
                raise BudgetUnavailable(day)
            request_id = uuid4().hex
            db.execute("INSERT INTO requests VALUES (?, ?, ?, NULL)", (request_id, day, self.reservation))
            db.execute("""INSERT INTO request_details(id,at,thread,batch_id,trigger,status)
                VALUES (?,?,?,?,?,'pending')""", (request_id, now.isoformat(), thread, batch_id, trigger))
            # An absent sort in the prepared preferences selects default routing.
            # An explicit null, an unrecognized value or missing preferences are unknown.
            provider = payload.get("provider")
            if isinstance(provider, dict):
                for route, sort in ROUTES.items():
                    if sort == provider.get("sort") and (sort is not None or "sort" not in provider):
                        db.execute("INSERT INTO request_routes VALUES (?,?)", (request_id, route))
                        break
        return request_id

    def settle(self, request_id: str, cost: Decimal) -> None:
        self.finish(request_id, cost, status="settled", evidence={}, ms=None, charge_source=None)

    def finish(self, request_id: str, cost: Decimal | None, *, status: str, evidence: dict,
               ms: int | None, charge_source: str | None) -> None:
        """Record observed usage with a confirmed cost, or keep the reservation if unknown."""
        actual = units(cost) if cost is not None else None
        counters = [evidence.get(key) for key in
                    ("input_tokens", "output_tokens", "cache_reads", "cache_writes", "reasoning_tokens")]
        with closing(sqlite3.connect(self.path)) as db, db:
            if actual is not None:
                row = db.execute("UPDATE requests SET actual=? WHERE id=? AND (actual IS NULL OR actual=?)",
                                 (actual, request_id, actual))
                if not row.rowcount:
                    raise ValueError("Unknown or conflicting OpenRouter settlement")
            # Only the first terminal observation owns the token counts and timing.
            # An explicit later settlement can independently resolve a retained hold.
            updated = db.execute("""UPDATE request_details SET status=?,input_tokens=?,output_tokens=?,cache_reads=?,
                cache_writes=?,reasoning_tokens=?,ms=?,charge_source=? WHERE id=? AND status='pending'""",
                       (status, *counters, ms, charge_source, request_id))
            if updated.rowcount:
                # A missing bill keeps its full reservation. Explicit OpenRouter
                # charges (including zero) retain the returned decimal precision.
                reported = str(cost) if cost is not None and charge_source == "openrouter" else None
                timings = [evidence.get(key) for key in TIMING_FIELDS]
                timings = [t if type(t) is int and 0 <= t < 2**63 else None for t in timings]
                outcome = evidence.get("request_outcome")
                observations = (dispatch_utc(evidence.get("dispatch_at")), *timings,
                                safe_provider(evidence.get("provider")), reported,
                                outcome if outcome in REQUEST_OUTCOMES else None)
                if any(v is not None for v in observations):
                    db.execute("""INSERT INTO request_metrics(request_id, dispatch_at, response_start_ms,
                                  first_token_ms, first_output_ms, response_end_ms, provider, reported_cost_usd,
                                  request_outcome) VALUES(?,?,?,?,?,?,?,?,?)""", (request_id, *observations))
            event = evidence.get("handoff_event")
            superseded = evidence.get("steer_acknowledged") or evidence.get("client_disconnected")
            if updated.rowcount and status == "completed" and not superseded and event in ("reminded", "tool_free"):
                db.execute("INSERT INTO handoff_events(request_id,event) VALUES (?,?)", (request_id, event))

    def today(self, *, day: str | None = None) -> dict:
        day = day or datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            spent, held, count = db.execute("""
                SELECT COALESCE(SUM(actual), 0),
                       COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END), 0),
                       COUNT(*) FROM requests WHERE day=?""", (day,)
            ).fetchone()
        return {"utc_day": day, "requests": count,
                "spent_usd": str(Decimal(spent) / UNITS_PER_DOLLAR),
                "reserved_usd": str(Decimal(held) / UNITS_PER_DOLLAR)}
