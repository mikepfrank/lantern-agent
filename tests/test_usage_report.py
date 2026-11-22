from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.budget import Budget
from lantern.usage_report import PROVIDER_GROUP_LIMIT, provider_summary, usage_report


DAY = "2026-11-19"


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory())
        self.state = Path(self.tmp)

    def legacy(self):
        with closing(sqlite3.connect(self.state / "usage.sqlite")) as db, db:
            db.execute("CREATE TABLE requests(id TEXT PRIMARY KEY, day TEXT, reserved INTEGER, actual INTEGER)")
            db.executemany("INSERT INTO requests VALUES(?,?,?,?)", [
                ("q1", DAY, 500_000_000, 50_000_000), ("q2", DAY, 500_000_000, None),
                ("tomorrow", "2026-11-20", 500_000_000, 300_000_000)])
        with closing(sqlite3.connect(self.state / "review-costs.sqlite")) as db, db:
            db.execute("""CREATE TABLE calls(id TEXT PRIMARY KEY, at TEXT, day TEXT, vendor TEXT, purpose TEXT,
                          reserved INTEGER, cost INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_reads INTEGER,
                          cache_writes INTEGER, ms INTEGER, status TEXT)""")
            db.executemany("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("l1", DAY + "T12:02Z", DAY, "luna", "spot", 20_000_000, 10_000_000, 100, 10, 80, 0, 100, "metered"),
                ("o1", DAY + "T12:03Z", DAY, "opus", "escalation", 50_000_000, 20_000_000, 60, 40, 0, 0, 500, "metered"),
                ("o2", DAY + "T12:04Z", DAY, "opus", "consult", 20_000_000, None, None, None, None, None, 200, "ambiguous")])

    def test_historical_reports_holds_and_unknown_fields(self):
        self.legacy()
        report = usage_report(self.state, DAY, calls=True)
        self.assertEqual(report["calls_without_batch_id_on_day"], {"qwen": 2, "luna": 1, "opus": 2})
        self.assertEqual(report["supervisor_to_qwen_settled_cost_ratio"], .6)
        qwen = report["by_model"]["qwen"]
        self.assertEqual((qwen["settled_spend_usd"], qwen["held_usd"], qwen["calls"]), ("0.05", "0.5", 2))
        self.assertIsNone(qwen["input_tokens"])
        self.assertEqual(qwen["calls_missing_token_counts"]["input_tokens"], 2)
        opus = report["by_model"]["opus"]
        self.assertEqual((opus["output_tokens"], opus["held_usd"], opus["calls_missing_token_counts"]["input_tokens"]),
                         (40, "0.02", 1))
        self.assertEqual([(r["model"], r["purpose"]) for r in report["reviews_by_purpose"]],
                         [("luna", "spot"), ("opus", "consult"), ("opus", "escalation")])
        self.assertEqual([r["id"] for r in report["calls"]], ["l1", "o1", "o2", "q1", "q2"])
        self.assertIsNone(report["calls"][-1]["settled_spend_usd"])
        self.assertIsNone(report["calls"][-1]["route"])
        self.assertNotIn("route", report["calls"][0])
        empty = usage_report(self.state, DAY, batch=3)
        self.assertEqual(empty["by_model"], {})
        self.assertIsNone(empty["supervisor_to_qwen_settled_cost_ratio"])
        self.assertIn("No calls were tagged", empty["batch_coverage"])

    def test_new_attribution_filters_and_reports_drilldown(self):
        self.legacy()
        with closing(sqlite3.connect(self.state / "usage.sqlite")) as db, db:
            db.execute("""CREATE TABLE request_details(id TEXT PRIMARY KEY, at TEXT, thread TEXT, batch_id INTEGER,
                          trigger TEXT, status TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_reads INTEGER,
                          cache_writes INTEGER, reasoning_tokens INTEGER, ms INTEGER, charge_source TEXT)""")
            db.execute("INSERT INTO request_details VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("q1", DAY + "T12:01Z", "thread", 5, "heartbeat", "completed", 50, 20, None,
                        None, 10, 110, "openrouter"))
        with closing(sqlite3.connect(self.state / "review-costs.sqlite")) as db, db:
            db.execute("ALTER TABLE calls ADD COLUMN thread_id TEXT")
            db.execute("ALTER TABLE calls ADD COLUMN batch_id INTEGER")
            db.execute("ALTER TABLE calls ADD COLUMN tool TEXT")
            db.execute("UPDATE calls SET thread_id='thread', batch_id=5 WHERE id='l1'")
            db.execute("UPDATE calls SET thread_id='thread', batch_id=6, tool='workspace_write' WHERE id='o1'")
        report = usage_report(self.state, DAY, batch=5, calls=True)
        self.assertEqual(report["calls_without_batch_id_on_day"], {"qwen": 1, "luna": 0, "opus": 1})
        self.assertEqual(set(report["by_model"]), {"luna", "qwen"})
        self.assertEqual(report["supervisor_to_qwen_settled_cost_ratio"], .2)
        self.assertEqual([r["id"] for r in report["calls"]], ["q1", "l1"])
        self.assertEqual(report["calls"][0]["charge_source"], "openrouter")
        self.assertIsNone(report["calls"][0]["route"])
        self.assertEqual(report["calls"][0]["trigger"], "heartbeat")
        self.assertIsNone(report["calls"][0]["cache_reads"])
        self.assertEqual(report["calls"][0]["reasoning_tokens"], 10)
        self.assertEqual(report["calls"][1]["charge_source"], "token_estimate")
        self.assertEqual(report["calls"][1]["cache_reads"], 80)
        self.assertEqual(usage_report(self.state, DAY, batch=6, calls=True)["calls"][0]["tool"], "workspace_write")

    def test_empty_is_read_only_and_bad_filters_are_rejected(self):
        report = usage_report(self.state, DAY)
        self.assertIsNone(report["supervisor_to_qwen_settled_cost_ratio"])
        self.assertNotIn("calls", report)
        self.assertEqual(list(self.state.iterdir()), [])
        for value in ("2026-02-30", "20261119", "not a date"):
            with self.assertRaises(ValueError):
                usage_report(self.state, value)
            with self.assertRaises(ValueError):
                provider_summary(self.state, value)
        for value in (0, True, -1):
            with self.assertRaises(ValueError):
                usage_report(self.state, DAY, batch=value)

    def test_provider_summary_spans_utc_days_and_limits_endpoint_groups(self):
        today = datetime.now(timezone.utc).date()
        earliest = today - timedelta(days=6)
        ledger = Budget(self.state / "usage.sqlite", Decimal(".1"))
        def record(day, sort, provider):
            rid = ledger.reserve({"provider": {"sort": sort}})
            ledger.finish(rid, Decimal(0), status="completed", evidence={"provider": provider},
                          ms=1, charge_source="openrouter")
            with closing(sqlite3.connect(ledger.path)) as db, db:
                db.execute("UPDATE requests SET day=? WHERE id=?", (day.isoformat(), rid))
        record(earliest - timedelta(days=1), "price", "outside")
        record(earliest, "price", "host0")
        record(today, "throughput", "host0")
        record(today, "price", "untrusted<name>")
        record(today, None, "unrouted")
        for i in range(PROVIDER_GROUP_LIMIT + 1):
            record(today, "price", f"host{i}")
        (self.state / "review-costs.sqlite").write_bytes(b"review ledger deliberately unavailable")
        snapshot = ledger.path.read_bytes()
        result = provider_summary(self.state)
        self.assertEqual((result["start_day"], result["end_day"]), (earliest.isoformat(), today.isoformat()))
        self.assertEqual((result["routed_calls_without_provider"], result["qwen_calls_without_route"]), (1, 1))
        self.assertEqual(result["qwen_by_route"]["cheapest"]["calls"], PROVIDER_GROUP_LIMIT + 3)
        pairs = result["qwen_by_provider_and_route"]
        self.assertEqual((len(pairs), result["omitted_provider_groups"], result["omitted_provider_calls"]),
                         (PROVIDER_GROUP_LIMIT, 2, 2))
        self.assertEqual((pairs[0]["provider"], pairs[0]["route"], pairs[0]["calls"]), ("host0", "cheapest", 2))
        self.assertEqual(provider_summary(self.state, earliest.isoformat())["qwen_by_route"]["cheapest"]["calls"], 1)
        self.assertEqual(ledger.path.read_bytes(), snapshot)

    def test_legacy_metrics_without_first_delta_stay_unknown_and_migrate(self):
        path = self.state / "usage.sqlite"
        ledger = Budget(path, Decimal(".1"))
        rid = ledger.reserve({"provider": {"sort": "price"}})
        ledger.finish(rid, Decimal(0), status="completed", evidence={"first_output_ms": 1}, ms=2, charge_source="openrouter")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE request_metrics DROP COLUMN first_token_ms")
        snapshot = path.read_bytes()
        legacy = provider_summary(self.state)["qwen_by_route"]["cheapest"]["completed_timings"]
        self.assertEqual((legacy["first_token_ms"]["samples"], legacy["first_output_ms"]["samples"]), (0, 1))
        self.assertEqual(path.read_bytes(), snapshot)
        upgraded = Budget(path, Decimal(".1"))
        rid = upgraded.reserve({"provider": {"sort": "price"}})
        upgraded.finish(rid, Decimal(0), status="completed", evidence={"first_token_ms": 0}, ms=2, charge_source="openrouter")
        timings = provider_summary(self.state)["qwen_by_route"]["cheapest"]["completed_timings"]
        self.assertEqual((timings["first_token_ms"]["samples"], timings["first_token_ms"]["mean_ms"]), (1, 0))

    def test_trial_charges_are_visible_by_exact_model_and_purpose(self):
        self.legacy()
        models = ("claude-3-opus-20240229", "claude-sonnet-4-6", "gpt-5.4")
        with closing(sqlite3.connect(self.state / "review-costs.sqlite")) as db, db:
            db.executemany("""INSERT INTO calls(id,day,vendor,purpose,reserved,cost,status)
                              VALUES(?,?,?,'reviewer_trial',5000000,1000000,'metered')""",
                           [(model, DAY, model) for model in models])
        report = usage_report(self.state, DAY)
        self.assertEqual(report["by_model"]["opus"]["calls"], 2)
        self.assertEqual(report["supervisor_to_qwen_settled_cost_ratio"], .66)
        trial_purposes = [p for p in report["reviews_by_purpose"] if p["purpose"] == "reviewer_trial"]
        self.assertEqual([p["model"] for p in trial_purposes], list(models))
        for model in models:
            entry = report["by_model"][model]
            self.assertEqual((entry["model_id"], entry["settled_spend_usd"], entry["calls"]), (model, "0.001", 1))
            self.assertEqual(report["calls_without_batch_id_on_day"][model], 1)
        self.assertEqual(usage_report(self.state, DAY, batch=5)["by_model"], {})

    def test_daily_room_rates_count_each_proposal_and_question_once(self):
        from lantern.usage_report import _room_review_rates

        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            db.execute("CREATE TABLE room(id INTEGER, thread_id TEXT)")
            db.execute("INSERT INTO room VALUES(1, 'private')")
            db.execute("CREATE TABLE operator_reviews(thread TEXT, asked_at TEXT)")
            db.executemany("INSERT INTO operator_reviews VALUES(?,?)",
                           [("private", DAY), ("sandbox", DAY), ("private", "2026-11-18")])
        screens = [{"vendor": "luna", "purpose": "tool", "thread_id": "private", "tool_call_id": call}
                   for call in ("a", "a", "b")]
        reviews = [{"vendor": "opus", "purpose": purpose, "thread_id": room}
                   for purpose, room in [("escalation", "private"), ("context_review", "private"),
                                         ("consult", "private"), ("escalation", "sandbox")]]
        rates = _room_review_rates(self.state, DAY, screens + reviews)
        self.assertEqual((rates["screened_proposals_with_id"], rates["questions_per_100_proposals"],
                          rates["opus_review_calls_per_100_proposals"]), (2, 50, 100))
        historical = _room_review_rates(self.state, DAY, screens + [dict(screens[0], tool_call_id=None)])
        self.assertIsNone(historical["questions_per_100_proposals"])
