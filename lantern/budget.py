"""Reserve before dispatch and settle against OpenRouter's reported USD cost."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import sqlite3
from uuid import uuid4


UNITS_PER_DOLLAR = 1_000_000_000
MODEL_CONTEXT_TOKENS = 1_024_000
DAILY_USD = Decimal("10")


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
            db.execute("""UPDATE request_details SET status=?,input_tokens=?,output_tokens=?,cache_reads=?,
                cache_writes=?,reasoning_tokens=?,ms=?,charge_source=? WHERE id=? AND status='pending'""",
                       (status, *counters, ms, charge_source, request_id))

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
