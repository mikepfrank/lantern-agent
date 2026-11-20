from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from lantern.luna import MODEL
from lantern.review_vendors import HTTPRejection, ReviewBudgetUnavailable, ReviewerVendors, VendorError, usage_cost


class ReviewVendorTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", OPENAI_API_KEY="fixture"))
        self.ledger = ReviewerVendors(self.state, Decimal("2"))
        self.payload = {"model": MODEL, "max_output_tokens": 512}

    def call(self):
        self.ledger.call("luna", self.payload, purpose="spot")

    def test_cache_and_long_input_pricing(self):
        usage = {"input_tokens": 300000, "output_tokens": 40,
                 "input_tokens_details": {"cached_tokens": 200000, "cache_write_tokens": 10000}}
        self.assertEqual(usage_cost("luna", usage)[-1], Decimal(".02453"))
        self.assertEqual(usage_cost("opus", {"input_tokens": 1000, "output_tokens": 20})[-1],
                         Decimal(".0165"))

    def test_one_day_grant_admits_one_more_request_and_expires_with_process_running(self):
        self.ledger = ReviewerVendors(self.state, Decimal(".001"))
        day = "2026-12-31"
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.executemany("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,cost,status)
                VALUES(?,?,?,'luna','spot',?,?,?)""",
                [("prior-cost", day, day, 800000, 750000, "metered"),
                 ("prior-hold", day, day, 100000, None, "ambiguous")])
        with patch("lantern.review_vendors.datetime") as clock, \
                patch("lantern.review_vendors.request_json", side_effect=OSError("lost reply")) as post:
            clock.now.return_value = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)
            with self.assertRaises(ReviewBudgetUnavailable):
                self.call()
            with closing(sqlite3.connect(self.ledger.path)) as db, db:
                db.execute("INSERT INTO review_daily_grants VALUES (?,?)", (day, 1500000))
            with self.assertRaises(VendorError):  # The dispatch reached the provider and left a hold.
                self.call()
            post.assert_called_once()
            self.assertEqual(self.ledger.today()["limit_usd"], "0.0025")
            self.assertEqual(self.ledger.today()["estimated_spend_usd"], "0.00075")
            self.assertGreater(Decimal(self.ledger.today()["reserved_usd"]), Decimal(".0001"))
            with self.assertRaises(ReviewBudgetUnavailable):
                self.call()
            clock.now.return_value = datetime(2027, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(ReviewBudgetUnavailable) as exhausted:
                self.call()
            self.assertEqual(exhausted.exception.day, "2027-01-01")
            self.assertEqual(self.ledger.today()["limit_usd"], "0.001")
            post.assert_called_once()
        restarted = ReviewerVendors(self.state, self.ledger.limit)
        self.assertEqual(restarted.today(day=day)["limit_usd"], "0.0025")

    def test_incomplete_response_stays_reserved_after_restart(self):
        with patch("lantern.review_vendors.request_json", return_value={"model":MODEL, "usage":{}}):
            with self.assertRaises(VendorError):
                self.call()
        today = ReviewerVendors(self.state, Decimal("2")).today()
        self.assertGreater(Decimal(today["reserved_usd"]), 0)
        self.assertEqual(today["estimated_spend_usd"], "0")

    def test_vision_reserves_for_model_window_before_contacting_luna(self):
        image_packet = {**self.payload, "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "Operator image 1"}, {"type": "input_image", "image_url": "data:image/png;base64,abcd"}]}]}
        restricted = ReviewerVendors(self.state, Decimal(".10"))
        with patch("lantern.review_vendors.request_json") as post:
            with self.assertRaises(ReviewBudgetUnavailable):
                restricted.call("luna", image_packet, purpose="spot")
            post.assert_not_called()
        with patch("lantern.review_vendors.request_json", return_value={
                "model": MODEL, "usage": {"input_tokens": 1200, "output_tokens": 15}}):
            self.ledger.call("luna", image_packet, purpose="spot")
        usage = self.ledger.today()
        self.assertEqual(usage["reserved_usd"], "0")
        self.assertEqual(Decimal(usage["estimated_spend_usd"]), Decimal(".0001275"))

    def test_only_known_rejections_release_reservation(self):
        with patch("lantern.review_vendors.request_json", side_effect=HTTPRejection(403)):
            with self.assertRaises(HTTPRejection):
                self.call()
        self.assertEqual(self.ledger.today()["reserved_usd"], "0")
        with patch("lantern.review_vendors.request_json", side_effect=HTTPRejection(502)):
            with self.assertRaises(HTTPRejection):
                self.call()
        self.assertGreater(Decimal(self.ledger.today()["reserved_usd"]), 0)

    def test_attribution_preserves_existing_history_and_outstanding_holds(self):
        old_state = self.state / "previous-release"
        old_state.mkdir()
        path = old_state / "review-costs.sqlite"
        now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE calls (
                id TEXT PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, vendor TEXT NOT NULL,
                purpose TEXT NOT NULL, reserved INTEGER NOT NULL, cost INTEGER, input_tokens INTEGER,
                output_tokens INTEGER, cache_reads INTEGER, cache_writes INTEGER, ms INTEGER, status TEXT NOT NULL)""")
            db.execute("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,status)
                          VALUES(?,?,?,'opus','consult',100000000,'ambiguous')""",
                       ("original", now.isoformat(), now.date().isoformat()))
        ledger = ReviewerVendors(old_state, Decimal("2"))
        response = {"model": MODEL, "usage": {"input_tokens": 100, "output_tokens": 12}}
        attribution = dict(thread_id="native-thread-fixture", batch_id=3, tool="workspace_write")
        with patch("lantern.review_vendors.request_json", return_value=response):
            ledger.call("luna", self.payload, purpose="tool", **attribution)
        with patch("lantern.review_vendors.request_json", side_effect=HTTPRejection(403)):
            with self.assertRaises(HTTPRejection):
                ledger.call("luna", self.payload, purpose="tool", **attribution)
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute("""SELECT thread_id,batch_id,tool,status,cost,input_tokens,output_tokens
                                 FROM calls ORDER BY rowid""").fetchall()
        self.assertEqual(rows, [
            (None, None, None, "ambiguous", None, None, None),
            ("native-thread-fixture", 3, "workspace_write", "metered", 16000, 100, 12),
            ("native-thread-fixture", 3, "workspace_write", "http_403", 0, None, None),
        ])
        report = ReviewerVendors(old_state, Decimal("2")).today()
        self.assertEqual(Decimal(report["estimated_spend_usd"]), Decimal(".000016"))
        self.assertEqual(Decimal(report["reserved_usd"]), Decimal(".10"))

    def test_shared_limit_serializes_concurrent_admission(self):
        self.ledger = ReviewerVendors(self.state, Decimal(".002"))
        with patch("lantern.review_vendors.request_json", side_effect=OSError("lost stream")) as post:
            def attempt(_):
                with self.assertRaises(VendorError):
                    self.call()
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(attempt, range(4)))
        self.assertEqual(1, post.call_count)
        self.assertLessEqual(Decimal(self.ledger.today()["reserved_usd"]), self.ledger.limit)
