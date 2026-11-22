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

    def turn(self, batch, status, date=DAY, time="00:05:00"):
        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            event = db.execute("INSERT INTO events(role,text,at) VALUES('system','fixture',?)",
                               (date + "T" + time + "+00:00",)).lastrowid
            db.execute("INSERT INTO batches(id,status,outcome) VALUES(?,?,?)", (batch, status, event))
            return event

    def inbox(self, batch, at, *, steered=False):
        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            event = db.execute("INSERT INTO events(role,text,at) VALUES('operator','fixture',?)", (at,)).lastrowid
            db.execute("INSERT INTO inbox(event_id,batch_id,steered) VALUES(?,?,?)", (event, batch, steered))

    def request(self, batch, price, provider="Nebius", sort="price", thread="live", day=DAY, time="00:04:00"):
        route = {} if sort is None else {"sort": sort}
        key = self.ledger.reserve({"provider": route}, thread=thread, batch_id=batch)
        cost = Decimal(price) if price is not None else None
        self.ledger.finish(key, cost, status="completed" if cost is not None else "interrupted",
                           evidence={"provider": provider}, ms=30, charge_source="openrouter")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("UPDATE requests SET day=? WHERE id=?", (day, key))
            db.execute("UPDATE request_details SET at=? WHERE id=?", (day + "T" + time + "+00:00", key))
        return key

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
        self.inbox(1, "2026-11-21T23:59:00+00:00")
        legacy = self.request(None, "0.01", day="2026-11-21")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("DELETE FROM request_details WHERE id=?", (legacy,))
        report = room_turn_metrics(self.state, DAY, DAY)
        self.assertEqual(report["unattributed_requests_in_period"], 1)
        self.assertIsNone(report["by_route"]["cheapest"]["usd_per_successful_turn"])
        self.assertEqual(report["overall"]["settled_qwen_usd"], "0.02")
        self.assertEqual(report["overall"]["minimum_usd_per_successful_turn"], "0.02")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("DROP TABLE request_details")
        self.assertIsNone(room_turn_metrics(self.state, DAY, DAY))

    def test_orphans_limit_only_the_turns_they_could_overlap(self):
        self.turn(1, "complete")
        self.inbox(1, DAY + "T00:00:00Z")
        self.request(1, "0.02")
        self.turn(2, "complete", time="00:09:00")
        self.inbox(2, DAY + "T00:01:00Z")  # This input was already queued during turn 1.
        self.request(2, "0.03", provider="B", sort="throughput", time="00:07:00")
        self.turn(4, "complete", time="01:05:00")
        self.inbox(4, DAY + "T01:00:00Z")
        self.request(4, "0.04", provider="C", sort=None, time="01:03:00")
        self.request(None, "0.01", time="00:03:00")
        self.request(2, "0.01", thread=None, time="00:03:00")
        self.request(3, "0.01", thread=None, time="00:03:00")  # No selected batch 3.
        self.request(None, "0.01", thread="other-room", time="00:03:00")
        report = room_turn_metrics(self.state, DAY, DAY)
        self.assertEqual(report["overall"]["possibly_unlinked_requests"], 2)
        self.assertEqual(report["unattributed_requests_in_period"], 2)
        self.assertEqual(report["by_route"]["cheapest"]["possibly_unlinked_requests"], 1)
        self.assertEqual(report["by_route"]["fastest"]["possibly_unlinked_requests"], 2)
        self.assertEqual(report["by_route"]["fastest"]["minimum_usd_per_successful_turn"], "0.03")
        self.assertEqual(report["by_route"]["default"]["usd_per_successful_turn"], "0.04")
        self.assertEqual({g["provider"]: g["possibly_unlinked_requests"]
                          for g in report["by_provider_and_route"]}, {"Nebius": 1, "B": 2, "C": 0})

    def test_only_legacy_day_known_and_no_original_input(self):
        previous = "2026-11-21"
        self.turn(1, "complete", previous)
        self.turn(2, "complete")
        self.turn(3, "complete")
        self.request(1, "0.02", day=previous)
        self.request(2, "0.03", sort="throughput")
        self.request(3, "0.04", sort=None)
        self.inbox(2, DAY + "T00:00:00Z")
        self.inbox(3, DAY + "T00:01:00Z", steered=True)
        orphan = self.request(None, "0.01", day=previous)
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("DELETE FROM request_details WHERE id=?", (orphan,))
        report = room_turn_metrics(self.state, previous, DAY)
        self.assertEqual(report["by_route"]["cheapest"]["possibly_unlinked_requests"], 1)
        self.assertEqual(report["by_route"]["fastest"]["usd_per_successful_turn"], "0.03")
        self.assertEqual(report["by_route"]["default"]["possibly_unlinked_requests"], 1)
        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            db.execute("ALTER TABLE inbox DROP COLUMN steered")
        report = room_turn_metrics(self.state, previous, DAY)
        self.assertEqual(report["by_route"]["fastest"]["possibly_unlinked_requests"], 1)

    def test_notification_outcome_can_precede_later_charges_across_midnight(self):
        previous = "2026-11-21"
        event = self.turn(1, "complete", previous, time="23:59:00")
        self.inbox(1, previous + "T23:58:00Z")
        self.request(1, "0.02", day=previous, time="23:58:30")
        with closing(sqlite3.connect(self.state / "chat.sqlite")) as db, db:
            db.execute("INSERT INTO notifications VALUES(1,'message',?)", (event,))
        self.request(None, "0.01", time="00:02:00")
        self.request(None, "0.01", time="02:00:00")
        report = room_turn_metrics(self.state, previous, previous)
        self.assertEqual(report["unattributed_requests_in_period"], 2)
        self.turn(2, "complete", time="01:00:00")
        report = room_turn_metrics(self.state, previous, previous)
        self.assertEqual(report["unattributed_requests_in_period"], 1)
        self.assertIsNone(report["overall"]["usd_per_successful_turn"])

    def test_recovered_links_preserve_native_links_and_actual_charges(self):
        self.turn(1, "complete")
        self.turn(2, "complete")
        old = self.request(None, "0.02", thread=None)
        partial = self.request(None, "0.02", provider="A", sort="throughput")
        native = self.request(2, "0.04", provider="B", sort=None)
        other = self.request(None, "0.08", thread="other-room")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("DELETE FROM request_details WHERE id=?", (old,))
            db.execute("""CREATE TABLE recovered_room_requests(request_id TEXT PRIMARY KEY,thread TEXT,
                          batch_id INTEGER,observed_at TEXT,route TEXT,provider TEXT)""")
            for key in (old, native, other):
                db.execute("INSERT INTO recovered_room_requests VALUES(?,'live',1,?,'fastest','A')",
                           (key, DAY + "T00:05:00Z"))
            db.execute("INSERT INTO recovered_room_requests VALUES(?,'live',1,?,'cheapest','other')",
                       (partial, DAY + "T00:05:00Z"))
        report = room_turn_metrics(self.state, DAY, DAY)
        self.assertEqual(report["unattributed_requests_in_period"], 0)
        self.assertEqual(report["overall"]["turns_without_receipts"], 0)
        self.assertEqual(report["overall"]["usd_per_successful_turn"], "0.04")
        self.assertEqual(report["by_route"]["fastest"]["usd_per_successful_turn"], "0.04")
        self.assertEqual({g["provider"] for g in report["by_provider_and_route"]}, {"A", "B"})
        self.assertEqual(report["by_route"]["default"]["usd_per_successful_turn"], "0.04")

    def test_audited_admission_counts_resolve_orphans_only_when_all_links_are_present(self):
        self.turn(1, "failed")
        self.turn(2, "failed")
        self.turn(3, "complete")
        self.request(2, "0.02")
        self.request(3, "0.03")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("""CREATE TABLE room_verified_request_counts(thread TEXT,batch_id INTEGER,
                       request_count INTEGER,PRIMARY KEY(thread,batch_id))""")
            db.executemany("INSERT INTO room_verified_request_counts VALUES('live',?,?)", [(1, 0), (2, 1), (3, 1)])
        self.request(None, "0.01")  # Within all three turn windows; the audit accounts for every request.
        report = room_turn_metrics(self.state, DAY, DAY)["overall"]
        self.assertEqual(report["confirmed_no_provider_turns"], 1)
        self.assertEqual(report["turns_without_receipts"], 0)
        self.assertEqual(report["possibly_unlinked_requests"], 0)
        self.assertEqual(report["usd_per_successful_turn"], "0.05")
        # The native charge wins if a discrepancy emerges, and exact coverage becomes unknown.
        self.request(1, "0.01")
        report = room_turn_metrics(self.state, DAY, DAY)["overall"]
        self.assertEqual(report["confirmed_no_provider_turns"], 0)
        self.assertEqual(report["receipt_count_mismatches"], 1)
        self.assertEqual(report["minimum_usd_per_successful_turn"], "0.06")
        self.assertEqual(report["possibly_unlinked_requests"], 1)
        self.assertIsNone(report["usd_per_successful_turn"])
        # An audited positive count with too few receipts is partial even absent any orphan.
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("UPDATE room_verified_request_counts SET request_count=2 WHERE batch_id=3")
            db.execute("UPDATE request_details SET thread='other' WHERE batch_id IS NULL")
        report = room_turn_metrics(self.state, DAY, DAY, batch=3)["overall"]
        self.assertEqual(report["receipt_count_mismatches"], 1)
        self.assertEqual(report["possibly_unlinked_requests"], 0)
        self.assertIsNone(report["usd_per_successful_turn"])
