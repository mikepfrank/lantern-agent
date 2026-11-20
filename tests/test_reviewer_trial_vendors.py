from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.review_vendors import ReviewerVendors
from reviewer_trial_vendors import GPT, SONNET, TrialVendors, _cost


class TrialBillingTests(unittest.TestCase):
    def setUp(self):
        state = Path(self.enterContext(TemporaryDirectory()))
        self.ledger = ReviewerVendors(state, Decimal("5"))
        self.enterContext(patch.dict("os.environ", {"OPENAI_API_KEY": "fixture", "ANTHROPIC_API_KEY": "fixture",
                                                   "LANTERN_REVIEW_DAILY_USD": "5"}))
        self.trial = TrialVendors(state, "reviewer-trial-test")
        self.send = self.enterContext(patch("reviewer_trial_vendors.request_json"))

    def test_pricing_and_single_dispatch_on_concurrent_attempts(self):
        self.send.return_value = {"model": "gpt-5.4-2026-03-05", "status": "completed",
                                  "output": [{"type": "reasoning"}, {"type": "message", "content": [
                                      {"type": "output_text", "text": "trial reply"}]}],
                                  "usage": {"input_tokens": 200, "output_tokens": 40,
                                            "input_tokens_details": {"cached_tokens": 100},
                                            "output_tokens_details": {"reasoning_tokens": 10}}}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.trial.run(GPT, "policy", "case", "same"), range(2)))
        self.assertCountEqual([r["status"] for r in results], ["ok", "already_attempted"])
        accepted = next(r for r in results if r["status"] == "ok")
        self.assertEqual((accepted["cost_usd"], accepted["reasoning_tokens"], accepted["text"]),
                         ("0.000875", 10, "trial reply"))
        self.send.assert_called_once()
        self.assertEqual(self.ledger.today()["estimated_spend_usd"], "0.000875")

    def test_ambiguous_response_holds_and_both_caps_include_holds(self):
        self.send.side_effect = TimeoutError()
        result = self.trial.run(SONNET, "policy", "case", "one")
        self.assertEqual(result["ledger_status"], "ambiguous")
        self.assertGreater(Decimal(result["held_usd"]), 0)
        self.trial.cap = 1
        self.assertEqual(self.trial.run(SONNET, "policy", "case", "two")["status"], "trial_budget")
        self.trial.cap = 950_000_000
        self.trial.ledger.limit = Decimal("0.000001")
        self.assertEqual(self.trial.run(SONNET, "policy", "case", "three")["status"], "daily_budget")
        self.send.assert_called_once()
        with closing(sqlite3.connect(self.ledger.path)) as db:
            self.assertEqual(db.execute("SELECT cost,thread_id FROM calls").fetchall(), [(None, self.trial.thread)])

    def test_anthropic_cache_components_are_individually_priced(self):
        counts, amount = _cost(SONNET, {"input_tokens": 100, "output_tokens": 30, "cache_read_input_tokens": 70,
                                        "cache_creation_input_tokens": 40,
                                        "cache_creation": {"ephemeral_5m_input_tokens": 25,
                                                           "ephemeral_1h_input_tokens": 15}})
        self.assertEqual(counts["input_tokens"], 210)
        self.assertEqual(amount, 954750)
