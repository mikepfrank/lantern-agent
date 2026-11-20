from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from lantern.budget import Budget, BudgetUnavailable, units
from lantern.independent_tools import BUDGET_TOOL, RESEARCH_TOOL, budget_status, can_start_request, web_search
from lantern.review_vendors import ReviewerVendors
from lantern.supervision import supervised_tools


class IndependentToolsTests(unittest.TestCase):
    def test_budget_and_search_schemas_support_the_required_review_rationale(self):
        budget, research, _ = supervised_tools([BUDGET_TOOL, RESEARCH_TOOL])
        self.assertEqual(budget["inputSchema"]["required"], ["rationale"])
        self.assertEqual(research["inputSchema"]["required"], ["query", "rationale"])

    def test_utc_status_counts_holds_reports_next_reset_and_rolls_both_ledgers(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            ledger = Budget(state / "usage.sqlite", Decimal("1"))
            vendors = ReviewerVendors(state, Decimal("5"))
            day = "2026-12-31"
            with closing(sqlite3.connect(ledger.path)) as db, db:
                db.executemany("INSERT INTO requests VALUES(?,?,?,?)",
                               [("held", day, units(Decimal("1")), None),
                                ("settled", day, units(Decimal("3")), units(Decimal("2.25")))])
            with closing(sqlite3.connect(vendors.path)) as db, db:
                db.execute("INSERT INTO review_daily_grants VALUES(?,?)", (day, units(Decimal("1.25"))))
                db.execute("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,status)
                              VALUES('test',?,?,'opus','consult',?,'pending')""",
                           (day, day, units(Decimal(".125"))))
            status = budget_status({}, ledger, vendors, now=datetime(2026, 12, 31, 23, 59, 59, 994000,
                                                                       tzinfo=timezone.utc))
            self.assertEqual(status["seconds_until_reset"], 1)
            self.assertEqual(status["reset_at_utc"], "2027-01-01T00:00:00+00:00")
            self.assertEqual(status["openrouter"]["available_usd"], "6.75")
            self.assertEqual(status["supervisors"]["limit_usd"], "6.25")
            self.assertEqual(status["supervisors"]["available_usd"], "6.125")
            after = budget_status({}, ledger, vendors, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(after["seconds_until_reset"], 86400)
            self.assertEqual(after["openrouter"]["available_usd"], "10")
            self.assertEqual(after["supervisors"]["available_usd"], "5")

    def test_preflight_uses_outstanding_reservations_and_conservative_next_request_cost(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            ledger = Budget(state / "usage.sqlite", Decimal("6"))
            vendors = ReviewerVendors(state, Decimal("5"))
            self.assertTrue(can_start_request(ledger, vendors))
            request = ledger.reserve({})
            self.assertFalse(can_start_request(ledger, vendors))
            with self.assertRaises(BudgetUnavailable) as denial:
                ledger.reserve({})
            self.assertEqual(denial.exception.day, datetime.now(timezone.utc).date().isoformat())
            ledger.settle(request, Decimal("0.002"))
            self.assertTrue(can_start_request(ledger, vendors))
            self.assertFalse(can_start_request(ledger, ReviewerVendors(state, Decimal("0"))))

    def test_general_search_limits_hits_returns_untrusted_text_and_never_follows_links(self):
        rows = [{"title": "public result", "content": "Text" * 200, "url": f"https://example.org/{i}"}
                for i in range(6)]
        rows.insert(0, {**rows[0], "url": "file:///some/path"})
        with patch("lantern.opus._json_get", return_value={"results": rows}) as fetch:
            result = web_search({"query": "Qwen 3.8 benchmarks"})
        self.assertEqual(result["source"], "mwmbl")
        hits = result["untrusted_search_results"]
        self.assertEqual(len(hits), 3)
        self.assertEqual([item["url"] for item in hits], [row["url"] for row in rows[1:4]])
        self.assertEqual(len(hits[0]["snippet"]), 400)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args, ("api.mwmbl.org", "/api/v2/search/?q=Qwen+3.8+benchmarks"))

    def test_query_validation_and_unavailable_index_return_actionable_errors(self):
        with patch("lantern.opus._json_get") as fetch:
            for query in ("https://private.example", "10.0.0.1", "sk-example-fake-key"):
                self.assertIn("error", web_search({"query": query}))
            with self.assertRaises(ValueError):
                web_search({"source": "https://private.example", "query": "example"})
            fetch.assert_not_called()
            fetch.side_effect = TimeoutError("private network diagnostic")
            result = web_search({"source": "wikipedia", "query": "endgames"})
            self.assertIn("unavailable", result["error"])
            self.assertNotIn("private", result["error"])
