"""Host-only pilot lifecycle and paired judgments. No external calls."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from lantern.chat_worker import WorkerBusy
from lantern.budget import units
from lantern.review_vendors import ReviewerVendors
from lantern.shadow_pilot import CASE_LIMIT, ShadowPilot, run_shadow


class ShadowPilotTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.now = datetime(2026, 11, 21, 2, tzinfo=timezone.utc)
        self.pilot = ShadowPilot(self.state, clock=lambda: self.now)
        self.sample = {"case_text": "private frozen case"}
        self.verdict = {"decision": "allow", "reason": "OPUS-PRIVATE"}

    def record(self, key, *, exclusion=None, status="ok", verdict=None):
        return self.pilot.record(key=key, thread_id="room", batch_id=11, tool="read_heritage",
                                 kind="action", sample=self.sample, exclusion=exclusion,
                                 opus={"status": status, "verdict": verdict or self.verdict, "error": None})

    def test_explicit_one_time_window_and_stop(self):
        self.assertFalse(self.record("inactive"))
        self.assertEqual(self.pilot.status()["state"], "not_started")
        self.assertFalse(self.pilot.directory.exists())
        start = self.pilot.activate()
        if os.name == "posix":
            self.assertEqual(self.pilot.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.pilot.directory.stat().st_mode & 0o777, 0o700)
        self.now += timedelta(days=2)
        self.assertEqual(self.pilot.activate()["deadline"], start["deadline"])
        self.assertTrue(self.record("one"))
        self.assertEqual(self.pilot.stop()["reason"], "operator_stop")
        self.assertEqual(self.pilot.results()[0]["sonnet"]["status"], "operator_stop")
        self.assertFalse(self.record("after-stop"))
        self.assertEqual(self.pilot.activate()["reason"], "operator_stop")
        self.assertEqual(self.pilot.drain_one(None), "closed")

    def test_expiry_prevents_dispatch_and_restarting_campaign(self):
        self.pilot.activate()
        self.record("held")
        self.now += timedelta(days=7)
        self.assertFalse(self.record("late"))
        self.assertEqual(self.pilot.drain_one(None), "closed")
        self.assertEqual(self.pilot.results()[0]["state"], "skipped")
        self.assertEqual(self.pilot.activate()["reason"], "expired")

    def test_exact_case_cap_and_resubmission(self):
        self.pilot.activate()
        def submit(i):
            return self.record("case-" + str(i % 25))
        with ThreadPoolExecutor(max_workers=3) as pool:
            count = sum(pool.map(submit, range(50)))
        # Under disk contention the live worker may skip a sample after its short
        # wait. Resubmission should fill remaining slots and never duplicate a case.
        self.assertGreater(count, 0)
        self.assertLessEqual(count, CASE_LIMIT)
        count += sum(submit(i) for i in range(25))
        self.assertEqual(count, CASE_LIMIT)
        sent = []
        def reviewer(request, deadline):
            self.assertNotIn("OPUS-PRIVATE", json.dumps(request))
            sent.append(request["request_id"])
            return {"status": "ok", "verdict": {"decision": "allow", "reason": "Independent reason"}}
        while self.pilot.drain_one(reviewer) == "processed":
            pass
        self.assertEqual(len(set(sent)), CASE_LIMIT)
        self.assertEqual(self.pilot.status()["paired"], {"agree": CASE_LIMIT})
        self.assertEqual(self.pilot.status()["reason"], "sample_limit")

    def test_busy_pilot_skips_sample_and_admits_resubmission_after_release(self):
        self.pilot.activate()
        with self.pilot._db(write=True), self.assertLogs("lantern.shadow_pilot", level="WARNING"):
            self.assertFalse(self.record("contended"))
        self.assertEqual(self.pilot.status()["enrolled"], 0)
        self.assertTrue(self.record("contended"))
        self.assertFalse(self.record("contended"))

    def test_frozen_packets_blinding_exclusions_and_failure_accounting(self):
        self.pilot.activate()
        self.record("research", exclusion="unmatched_research", status="excluded_research")
        self.record("malformed", status="invalid_opus")
        self.record("allow")
        self.assertFalse(self.record("allow"))
        self.sample["case_text"] = "later mutation"
        denied = {"decision": "deny", "reason": "private research rationale"}
        def reviewer(request, deadline):
            self.assertEqual(request["sample"], {"case_text": "private frozen case"})
            self.assertNotIn("OPUS-PRIVATE", json.dumps(request))
            self.assertEqual(deadline, self.now + timedelta(days=7))
            return {"status": "ok", "verdict": denied}
        for expected in ("processed", "processed", "idle"):
            self.assertEqual(self.pilot.drain_one(reviewer), expected)
        rows = self.pilot.results(include_evidence=True)
        self.assertIsNone(rows[0]["sample"])
        self.assertIsNone(rows[1]["comparison"])
        self.assertEqual(rows[2]["comparison"], "disagree")
        self.assertEqual(rows[2]["sonnet"]["verdict"], denied)
        self.assertNotIn("sample", self.pilot.results()[2])
        status = self.pilot.status()
        self.assertEqual(status["enrolled"], 2)
        self.assertEqual(status["excluded"], {"unmatched_research": 1})
        self.assertEqual(status["paired"], {"disagree": 1})
        for private in ("OPUS-PRIVATE", "private frozen case", "private research rationale"):
            self.assertNotIn(private, json.dumps(status))

    def test_scopes_and_ids_contribute_to_agreement(self):
        self.pilot.activate()
        a = {"id": 4, "decision": "watch", "reason": "first", "tools": ["read", "write"]}
        b = {"id": 7, "decision": "retire", "reason": "settled", "tools": []}
        reference = {**self.verdict, "concerns": [a, b]}
        for key in ("same_scope", "different_scope"):
            self.record(key, verdict=reference)
        for scope in (["write","read"],["read"]):
            self.pilot.drain_one(lambda *args: {"status": "ok", "verdict": {**self.verdict,
                "concerns": [b, {**a, "tools": scope, "reason": "independent"}]}})
        self.assertEqual(self.pilot.status()["paired"], {"agree": 1, "disagree": 1})

    def test_operator_question_counts_include_action_and_context_cases(self):
        self.pilot.activate()
        self.record("action", verdict={"decision": "ask_operator", "reason": "Destination?"})
        self.record("context", verdict={"concern": {"id": 1, "decision": "watch", "tools": ["write"],
                                                   "reason": "Unresolved"}})
        self.pilot.drain_one(lambda *_: {"status": "ok", "verdict": self.verdict})
        self.pilot.drain_one(lambda *_: {"status": "ok", "verdict": {"concern": {
            "id": 1, "decision": "ask_operator", "reason": "Intent?", "tools": ["write"]}}})
        self.assertEqual(self.pilot.status()["operator_questions"], {"opus": 1, "sonnet": 1})

    def test_restart_checks_ledger_before_dispatch(self):
        self.pilot.activate()
        ledger = ReviewerVendors(self.state, Decimal(5))
        for key in ("before_dispatch", "possibly_sent"):
            self.record(key)
        ids = {r["opus_id"]: r["shadow_id"] for r in self.pilot.results()}
        with self.pilot._db(write=True) as db:
            db.execute("UPDATE cases SET state='working'")
        with closing(sqlite3.connect(ledger.path)) as db, db:
            db.execute("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,status)
                VALUES(?,'now','2026-11-21','test','shadow_review',10000000,'ambiguous')""", (ids["possibly_sent"],))
        self.pilot._recover()
        self.assertEqual(self.pilot.status()["held_usd"], "0.01")
        calls = []
        def reviewer(request, deadline):
            calls.append(request["request_id"])
            raise ValueError("private text")
        self.assertEqual(self.pilot.drain_one(reviewer), "processed")
        self.assertEqual(self.pilot.drain_one(reviewer), "idle")
        self.assertEqual(calls, [ids["before_dispatch"]])
        outcomes = self.pilot.status()["sonnet_outcomes"]
        self.assertEqual(outcomes, {"interrupted_unknown_result": 1, "shadow_error": 1})

    def test_spend_limit_closes_queued_work(self):
        self.pilot.activate()
        self.record("one")
        self.record("two")
        self.assertEqual(self.pilot.drain_one(lambda *_: {"status": "pilot_cap"}), "closed")
        self.assertFalse(self.record("late"))
        self.assertEqual(self.pilot.status()["reason"], "pilot_cap")
        self.assertEqual(self.pilot.status()["case_states"], {"finished": 1, "skipped": 1})

    def test_extension_adds_only_new_cases_and_spend_across_restarts(self):
        self.pilot.activate()
        self.record("first")
        self.record("second")
        self.record("cap")
        self.record("skipped")
        for _ in range(2):
            self.pilot.drain_one(lambda *_: {"status": "ok", "verdict": self.verdict})
        self.assertEqual(self.pilot.drain_one(lambda *_: {"status": "pilot_cap"}), "closed")
        old = self.pilot.results()
        ledger = ReviewerVendors(self.state, Decimal(5))
        with closing(sqlite3.connect(ledger.path)) as db, db:
            db.executemany("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,cost,status)
                VALUES(?,'now','2026-11-20','test','shadow_review',?,?,?)""", [
                (old[0]["shadow_id"], units(Decimal(".25")), units(Decimal(".20")), "metered"),
                (old[1]["shadow_id"], units(Decimal(".10")), None, "ambiguous")])
        old = self.pilot.results()
        until = (self.now + timedelta(hours=12)).isoformat()
        report = self.pilot.extend(until)
        self.assertEqual((report["case_limit"], report["limit_usd"], report["cost_usd"], report["held_usd"]),
                         (14, "0.8", "0.2", "0.1"))
        self.pilot = ShadowPilot(self.state, clock=lambda: self.now)
        self.assertFalse(self.record("skipped"))
        for i in range(10):
            self.assertTrue(self.record(f"new-{i}"))
        self.assertFalse(self.record("extra"))
        same_until = "2026-11-21T08:00:00-06:00"
        self.assertEqual(self.pilot.extend(same_until)["case_limit"], 14)
        self.assertEqual(self.pilot.activate()["deadline"], until)
        seen = []
        def judge(row, deadline):
            seen.append(row["request_id"])
            self.assertEqual((row["case_limit"], row["limit_usd"], deadline.isoformat()), (14, "0.8", until))
            return {"status": "ok", "verdict": self.verdict}
        for _ in range(10):
            self.assertEqual(self.pilot.drain_one(judge), "processed")
        self.assertEqual(self.pilot.drain_one(judge), "closed")
        self.assertEqual(self.pilot.extend(until)["reason"], "sample_limit")
        self.assertEqual(self.pilot.status()["paired"], {"agree": 12})
        self.assertEqual(len(set(seen)), 10)
        self.assertEqual(self.pilot.results()[:4], old)

    def test_extension_deadline_and_active_campaign_guards(self):
        until = self.now + timedelta(minutes=5)
        with self.assertRaises(ValueError):
            self.pilot.extend(until.isoformat())
        self.pilot.activate()
        with self.assertRaises(ValueError):
            self.pilot.extend(until.isoformat())
        self.pilot.stop()
        for invalid in (self.now.isoformat(), until.replace(tzinfo=None).isoformat(),
                        (self.now + timedelta(days=2)).isoformat()):
            with self.assertRaises(ValueError):
                self.pilot.extend(invalid)
        self.pilot.extend(until.isoformat())
        self.record("right before cutoff")
        self.now = until
        self.assertFalse(self.record("too late"))
        self.assertEqual(self.pilot.drain_one(None), "closed")
        self.assertEqual(self.pilot.extend(until.isoformat())["reason"], "expired")

    def test_live_recorder_can_enqueue_while_consumer_is_waiting_on_provider(self):
        self.pilot = ShadowPilot(self.state)
        self.pilot.activate()
        self.record("original")
        started, release, cancelled = Event(), Event(), Event()
        def slow_reviewer(row, deadline):
            started.set()
            release.wait(10)
            return {"status": "ok", "verdict": self.verdict}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_shadow, self.state, slow_reviewer, stop=cancelled, poll_seconds=0.01)
            try:
                self.assertTrue(started.wait(5))
                self.assertTrue(self.record("arrived-during-call"))
                with self.assertRaises(WorkerBusy):
                    run_shadow(self.state, slow_reviewer, stop=cancelled)
                self.assertEqual(self.pilot.stop()["case_states"], {"working": 1, "skipped": 1})
            finally:
                cancelled.set()
                release.set()
            future.result(timeout=5)
        self.assertEqual(self.pilot.status()["case_states"], {"finished": 1, "skipped": 1})
