from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.budget import Budget
from lantern.chat_store import ChatStore
from lantern.turn_metrics import room_turn_metrics


DAY = "2026-11-22"


class TurnMetricsTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        ChatStore(self.state)
        self.ledger = Budget(self.state / "usage.sqlite", Decimal("0.5"))
        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            db.execute("UPDATE room SET thread_id='live' WHERE id=1")

    def turn(self, batch, status, date=DAY):
        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            event = db.execute("INSERT INTO events(role,text,at) VALUES('system','fixture',?)",
                               (date + "T00:05:00+00:00",)).lastrowid
            db.execute("INSERT INTO batches(id,status,outcome) VALUES(?,?,?)", (batch, status, event))

    def request(self, batch, price, provider="Nebius", sort="price", thread="live", day=DAY):
        route = {} if sort is None else {"sort": sort}
        key = self.ledger.reserve({"provider": route}, thread=thread, batch_id=batch)
        cost = Decimal(price) if price is not None else None
        self.ledger.finish(key, cost, status="completed" if cost is not None else "interrupted",
                           evidence={"provider": provider}, ms=30, charge_source="openrouter")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("UPDATE requests SET day=? WHERE id=?", (day, key))

    def test_costs_whole_turn_across_midnight_and_includes_failed_spend(self):
        self.turn(1, "complete")
        self.turn(2, "failed")
        self.turn(3, "deferred")
        self.turn(4, "interrupted")
        self.turn(5, "active")
        self.turn(6, "complete", "2026-11-23")
        self.request(1, "0.01", day="2026-11-21")
        self.request(1, "0.03")
        self.request(1, "0.9", thread="separate-test-room")
        self.request(2, "0.02")
        for batch in (3, 4, 5, 6):
            self.request(batch, "0.1")
        report = room_turn_metrics(self.state, DAY, DAY)
        group = report["by_provider_and_route"][0]
        self.assertEqual((group["provider"], group["route"]), ("Nebius", "cheapest"))
        self.assertEqual((group["successful_turns"], group["failed_turns"]), (1, 1))
        self.assertEqual(group["usd_per_successful_turn"], "0.06")
        self.assertNotIn("minimum_usd_per_successful_turn", group)
        self.assertEqual(report["overall"], report["by_route"]["cheapest"])
        self.assertEqual(room_turn_metrics(self.state, DAY, DAY, batch=1)["overall"]["usd_per_successful_turn"],
                         "0.04")

    def test_mixed_providers_unknown_cost_and_unlinked_failures(self):
        self.turn(1, "complete")
        self.turn(2, "failed")
        self.turn(3, "failed")
        self.request(1, "0.02")
        self.request(1, "0.01", provider="Parasail", sort=None)
        self.request(2, None, provider="Parasail", sort=None)
        report = room_turn_metrics(self.state, DAY, DAY)
        all_turns = report["overall"]
        self.assertEqual((all_turns["successful_turns"], all_turns["failed_turns"]), (1, 2))
        self.assertEqual(all_turns["unsettled_calls"], 1)
        self.assertEqual(all_turns["turns_without_receipts"], 1)
        self.assertEqual(all_turns["settled_qwen_usd"], "0.03")
        self.assertIsNone(all_turns["usd_per_successful_turn"])
        self.assertEqual(all_turns["minimum_usd_per_successful_turn"], "0.03")
        groups = {(g["provider"], g["route"]): g for g in report["by_provider_and_route"]}
        self.assertEqual(groups["mixed", "mixed"]["usd_per_successful_turn"], "0.03")
        self.assertEqual(groups["Parasail", "default"]["successful_turns"], 0)
        self.assertNotIn("minimum_usd_per_successful_turn", groups["Parasail", "default"])
        self.assertIn(("unknown", "unknown"), groups)
        report = room_turn_metrics(self.state, DAY, DAY, provider_limit=1)
        self.assertEqual(len(report["by_provider_and_route"]), 1)
        self.assertEqual(report["omitted_provider_turns"], 2)

    def test_missing_or_unassignable_legacy_data_does_not_lower_average(self):
        with TemporaryDirectory() as absent:
            self.assertIsNone(room_turn_metrics(Path(absent), DAY, DAY))
            self.assertEqual(list(Path(absent).iterdir()), [])
        self.turn(1, "complete")
        self.request(1, "0.02")
        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            first = db.execute("INSERT INTO events(role,text,at) VALUES('operator','fixture',?)",
                               ("2026-11-21T23:59:00+00:00",)).lastrowid
            db.execute("INSERT INTO inbox(event_id,batch_id) VALUES(?,1)", (first,))
        self.request(None, "0.01", day="2026-11-21")
        report = room_turn_metrics(self.state, DAY, DAY)
        self.assertEqual(report["unattributed_requests_in_period"], 1)
        self.assertIsNone(report["by_route"]["cheapest"]["usd_per_successful_turn"])
        self.assertEqual(report["overall"]["settled_qwen_usd"], "0.02")
        self.assertEqual(report["overall"]["minimum_usd_per_successful_turn"], "0.02")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("DROP TABLE request_details")
        self.assertIsNone(room_turn_metrics(self.state, DAY, DAY))
