from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lantern.budget import Budget


class BudgetTests(unittest.TestCase):
    def test_handoff_reminder_survives_restart_and_rearms_for_new_context(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Budget(Path(folder) / "usage.sqlite", Decimal(".1"))

            def record(inputs, outputs=0, *, mark=None, status="completed", **delivery):
                receipt = ledger.reserve({}, thread="room")
                evidence = {"input_tokens": inputs, "output_tokens": outputs, "handoff_event": mark, **delivery}
                ledger.finish(receipt, Decimal(0), status=status, evidence=evidence, ms=1, charge_source=None)

            record(179_990, 9)
            record(179_990, 9)
            self.assertFalse(ledger.handoff_reminder_due("room"))
            record(179_990, 10)
            with closing(sqlite3.connect(ledger.path)) as db, db:
                db.execute("UPDATE requests SET day='2020-01-01'")
                db.execute("UPDATE request_details SET at='2020-01-01T12:30:00+00:00'")
            ledger = Budget(ledger.path, Decimal(".1"))
            self.assertTrue(ledger.handoff_reminder_due("room"))
            self.assertFalse(ledger.handoff_reminder_due("different"))

            record(None, mark="reminded", status="review_blocked")
            self.assertTrue(ledger.handoff_reminder_due("room"))
            record(180_001, mark="reminded", steer_acknowledged=True)
            self.assertTrue(ledger.handoff_reminder_due("room"))
            record(180_001, mark="reminded", client_disconnected=True)
            self.assertTrue(ledger.handoff_reminder_due("room"))
            record(181_000, mark="reminded")
            record(182_000)
            self.assertFalse(ledger.handoff_reminder_due("room"))
            record(190_000, mark="tool_free")
            self.assertFalse(ledger.handoff_reminder_due("room"))
            record(180_001)
            self.assertTrue(ledger.handoff_reminder_due("room"))
            record(180_001, mark="reminded")
            record(70_000)
            self.assertFalse(ledger.handoff_reminder_due("room"))
            record(180_001)
            self.assertTrue(ledger.handoff_reminder_due("room"))
            record(70_000, mark="reminded")
            self.assertFalse(ledger.handoff_reminder_due("room"))
            record(180_001)
            self.assertTrue(ledger.handoff_reminder_due("room"))

    def test_late_completion_cannot_revive_an_old_context(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Budget(Path(folder) / "usage.sqlite", Decimal(".1"))
            earlier = ledger.reserve({}, thread="room")
            later = ledger.reserve({}, thread="room")
            for receipt, tokens in [(later, 40_000), (earlier, 190_000)]:
                ledger.finish(receipt, Decimal(0), status="completed", evidence={"input_tokens": tokens},
                              ms=5, charge_source=None)
            self.assertFalse(ledger.handoff_reminder_due("room"))

    def test_detailed_accounting_preserves_legacy_charges_and_unreported_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE requests(id TEXT PRIMARY KEY,day TEXT,reserved INTEGER,actual INTEGER)")
                db.execute("INSERT INTO requests VALUES('old','2026-11-19',1000,5)")
            ledger = Budget(path, Decimal("1"))
            request = ledger.reserve({"input": "a private prompt"}, thread="native", batch_id=17, trigger="alarm")
            ledger.finish(request, Decimal(".005"), status="completed", evidence={"input_tokens": 390,
                          "output_tokens": 88, "cache_reads": 310, "reasoning_tokens": 20},
                          ms=512, charge_source="openrouter")
            ledger.settle(request, Decimal(".005"))
            with self.assertRaisesRegex(ValueError, "conflicting"):
                ledger.settle(request, Decimal(".5"))
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(("old", 5, None), db.execute("""SELECT r.id,r.actual,d.at FROM requests r
                    LEFT JOIN request_details d USING(id) WHERE r.id='old'""").fetchone())
                self.assertEqual((5000000, "native", 17, "alarm", "completed", 390, 88, 310, None, 20, 512, "openrouter"),
                                 db.execute("""SELECT actual,thread,batch_id,trigger,status,input_tokens,
                    output_tokens,cache_reads,cache_writes,reasoning_tokens,ms,charge_source
                    FROM requests JOIN request_details USING(id) WHERE id=?""", (request,)).fetchone())

    def test_concurrent_reservations_cannot_exceed_daily_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Budget(Path(folder) / "usage.db", Decimal("3"))

            def attempt(_):
                try:
                    return ledger.reserve({})
                except ValueError:
                    return None

            with ThreadPoolExecutor(max_workers=5) as pool:
                successes = list(filter(None, pool.map(attempt, range(5))))
            self.assertEqual(3, len(successes))
            ledger.settle(successes[0], Decimal("0.0123456789"))
            ledger.settle(successes[0], Decimal("0.0123456789"))
            recovered = Budget(ledger.path, Decimal("3"))
            self.assertEqual("6", recovered.today()["reserved_usd"])
            self.assertEqual("0.012345679", recovered.today()["spent_usd"])
            self.assertTrue(recovered.reserve({}))
