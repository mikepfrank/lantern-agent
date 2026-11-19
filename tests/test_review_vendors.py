from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lantern.luna import MODEL
from lantern.review_vendors import HTTPRejection, ReviewerVendors, VendorError, usage_cost


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

    def test_incomplete_response_stays_reserved_after_restart(self):
        with patch("lantern.review_vendors.request_json", return_value={"model":MODEL, "usage":{}}):
            with self.assertRaises(VendorError):
                self.call()
        today = ReviewerVendors(self.state, Decimal("2")).today()
        self.assertGreater(Decimal(today["reserved_usd"]), 0)
        self.assertEqual(today["estimated_spend_usd"], "0")

    def test_only_known_rejections_release_reservation(self):
        with patch("lantern.review_vendors.request_json", side_effect=HTTPRejection(403)):
            with self.assertRaises(HTTPRejection):
                self.call()
        self.assertEqual(self.ledger.today()["reserved_usd"], "0")
        with patch("lantern.review_vendors.request_json", side_effect=HTTPRejection(502)):
            with self.assertRaises(HTTPRejection):
                self.call()
        self.assertGreater(Decimal(self.ledger.today()["reserved_usd"]), 0)

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
