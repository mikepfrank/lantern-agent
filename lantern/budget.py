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

    def reserve(self, payload: dict) -> str:
        # Independent of token estimates: reserve for the model's full context
        # at the per-token price ceilings applied by the gateway.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            day = datetime.now(timezone.utc).date().isoformat()
            used, = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual, reserved)), 0) FROM requests WHERE day=?",
                (day,),
            ).fetchone()
            if used + self.reservation > units(DAILY_USD):
                raise ValueError("OpenRouter daily budget has insufficient unreserved funds")
            request_id = uuid4().hex
            db.execute("INSERT INTO requests VALUES (?, ?, ?, NULL)", (request_id, day, self.reservation))
        return request_id

    def settle(self, request_id: str, cost: Decimal) -> None:
        actual = units(cost)
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("UPDATE requests SET actual=? WHERE id=? AND actual IS NULL",
                             (actual, request_id))
            if not row.rowcount:
                previous = db.execute("SELECT actual FROM requests WHERE id=?", (request_id,)).fetchone()
                if previous != (actual,):
                    raise ValueError("Unknown or conflicting OpenRouter settlement")

    def today(self) -> dict:
        day = datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            spent, held, count = db.execute("""
                SELECT COALESCE(SUM(actual), 0),
                       COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END), 0),
                       COUNT(*) FROM requests WHERE day=?""", (day,)
            ).fetchone()
        return {"utc_day": day, "requests": count,
                "spent_usd": str(Decimal(spent) / UNITS_PER_DOLLAR),
                "reserved_usd": str(Decimal(held) / UNITS_PER_DOLLAR)}
