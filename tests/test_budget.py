from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lantern.budget import Budget, BudgetUnavailable
from lantern.diagnostics import read_diagnostics
from lantern.usage_report import usage_report


class BudgetTests(unittest.TestCase):
    def test_routing_measurements_distinguish_missing_terminal_and_unbilled_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.sqlite"
            ledger = Budget(path, Decimal("3"))
            evidence = {"dispatch_at": "2026-11-21T03:04:05.123456+00:00", "response_start_ms": 30,
                        "first_token_ms": 100, "first_output_ms": 200, "response_end_ms": 1000,
                        "input_tokens": 80, "output_tokens": 20, "provider": "Host A"}
            billed = ledger.reserve({"provider": {"sort": "price"}})
            ledger.finish(billed, Decimal("0.0010000004"), status="completed", evidence={**evidence, "cache_reads": 60},
                          ms=2001, charge_source="openrouter")
            free = ledger.reserve({"provider": {"sort": "price"}})
            ledger.finish(free, Decimal("0"), status="completed",
                          evidence={**evidence, "first_token_ms": 2000, "response_end_ms": 3000, "first_output_ms": None},
                          ms=4000, charge_source="openrouter")
            held = ledger.reserve({"provider": {"sort": "price"}})
            ledger.finish(held, None, status="interrupted", evidence={
                "dispatch_at": evidence["dispatch_at"], "provider": "private\ncontent", "response_start_ms": 11},
                ms=40, charge_source=None)
            rejected = ledger.reserve({"provider": {"sort": "throughput"}})
            ledger.finish(rejected, Decimal("0"), status="upstream_http_error", evidence={},
                          ms=27, charge_source="router_no_eligible_endpoint")
            # A duplicate finalization cannot replace the first stream measurements.
            ledger.finish(billed, Decimal("0.0010000004"), status="completed", evidence={**evidence, "response_end_ms": 1},
                          ms=1, charge_source="openrouter")
            with closing(sqlite3.connect(path)) as db:
                day, = db.execute("SELECT day FROM requests WHERE id=?", (billed,)).fetchone()
                self.assertEqual(db.execute("SELECT actual FROM requests WHERE id=?", (held,)).fetchone(), (None,))
                self.assertEqual(db.execute("SELECT actual FROM requests WHERE id=?", (billed,)).fetchone(), (1_000_001,))
            report = usage_report(path.parent, day, calls=True)
            calls = {r["id"]: r for r in report["calls"]}
            self.assertEqual(calls[billed]["reported_cost_usd"], "0.0010000004")
            self.assertEqual(calls[free]["reported_cost_usd"], "0")
            self.assertIsNone(calls[held]["reported_cost_usd"])
            self.assertIsNone(calls[rejected]["reported_cost_usd"])
            self.assertIsNone(calls[held]["response_end_ms"])
            self.assertIsNone(calls[held]["provider"])
            self.assertEqual(calls[held]["held_usd"], "3")
            cheap = report["qwen_by_route"]["cheapest"]
            self.assertEqual((cheap["calls"], cheap["completed_calls"], cheap["reported_cost_calls"],
                              cheap["reported_cost_usd"]), (3, 2, 2, "0.0010000004"))
            self.assertEqual(cheap["completed_timings"]["response_end_ms"],
                             {"samples": 2, "mean_ms": 2000, "median_ms": 2000})
            self.assertEqual(cheap["completed_timings"]["first_output_ms"]["samples"], 1)
            self.assertEqual(cheap["completed_timings"]["first_token_ms"], {"samples": 2, "mean_ms": 1050, "median_ms": 1050})
            self.assertEqual((cheap["decoding_samples"], cheap["decoding_output_tokens_per_second"]), (2, 20))
            self.assertEqual((cheap["priced_token_samples"], cheap["priced_tokens"],
                              cheap["calls_without_paired_cost_and_tokens"]), (2, 200, 1))
            self.assertEqual(Decimal(cheap["usd_per_token"]), Decimal("0.0010000004") / 200)
            self.assertEqual((cheap["priced_input_tokens"], cheap["priced_output_tokens"],
                              cheap["priced_cached_input_tokens"], cheap["priced_cache_read_samples"]), (160, 40, 60, 1))
            self.assertEqual(Decimal(cheap["usd_per_million_tokens"]), Decimal("5.000002"))
            self.assertEqual((cheap["throughput_samples"], cheap["effective_output_tokens_per_second"]), (2, 10))
            fast = report["qwen_by_route"]["fastest"]
            self.assertEqual(fast["completed_timings"]["response_start_ms"]["samples"], 0)
            self.assertIsNone(fast["reported_cost_usd"])
            logs = {r["ref"]: r for r in read_diagnostics(path.parent, {})["records"]}
            for key in ("dispatch_at", "response_start_ms", "first_token_ms", "first_output_ms", "response_end_ms", "provider"):
                self.assertEqual(logs["qwen:" + billed][key], evidence[key])
            self.assertEqual(logs["qwen:" + free]["reported_cost_usd"], "0")
            self.assertNotIn("response_end_ms", logs["qwen:" + held])
            self.assertNotIn("provider", logs["qwen:" + held])

    def test_route_receipts_share_one_cap_and_keep_only_known_sorts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.sqlite"
            ledger = Budget(path, Decimal("3"))
            ids = [ledger.reserve({"provider": {"sort": sort}, "input": "private prompt"}, trigger="heartbeat")
                   for sort in ("price", "throughput", "unknown provider text")]
            with self.assertRaises(BudgetUnavailable):
                ledger.reserve({"provider": {"sort": "price"}})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(dict(db.execute("SELECT request_id,route FROM request_routes")),
                                 {ids[0]: "cheapest", ids[1]: "fastest"})
                day, = db.execute("SELECT day FROM requests WHERE id=?", (ids[0],)).fetchone()
            calls = usage_report(path.parent, day, calls=True)["calls"]
            self.assertEqual({row["id"]: row["route"] for row in calls},
                             {ids[0]: "cheapest", ids[1]: "fastest", ids[2]: None})

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
