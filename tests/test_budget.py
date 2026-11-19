from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from lantern.budget import Budget


class BudgetTests(unittest.TestCase):
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
