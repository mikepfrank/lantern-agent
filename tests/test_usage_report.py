from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.usage_report import usage_report


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
        for value in (0, True, -1):
            with self.assertRaises(ValueError):
                usage_report(self.state, DAY, batch=value)

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
