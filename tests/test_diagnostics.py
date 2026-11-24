from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from lantern.budget import Budget
from lantern.chat_store import ChatStore
from lantern.diagnostics import read_diagnostics
from lantern.review_vendors import ReviewerVendors
from lantern.supervision import Supervisor


class DiagnosticsTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def sql(self, name, statement, args=()):
        with closing(sqlite3.connect(self.state / name)) as db, db:
            db.execute(statement, args)

    def test_empty_state_is_not_created(self):
        absent = self.state / "missing"
        reply = read_diagnostics(absent, {})
        self.assertEqual(reply["records"], [])
        self.assertFalse(reply["has_more"])
        self.assertFalse(absent.exists())
        for args in ({"filename": "other.db"}, {"limit": 31}, {"limit": True}, {"limit": 0},
                     {"batch_id": -1}, {"batch_id": 2**64}, {"before": "../secret"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                read_diagnostics(absent, args)

    def test_historical_and_partial_state_stays_read_only(self):
        self.sql("usage.sqlite", "CREATE TABLE requests(id,day,reserved,actual)")
        self.sql("review-costs.sqlite", "CREATE TABLE calls(id)")
        (self.state / "supervision.sqlite").write_bytes(b"a corrupt file, private details")
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        reply = read_diagnostics(self.state, {})
        self.assertEqual(reply["records"], [])
        self.assertIn("review", reply["unavailable_sections"])
        self.assertIn("supervision", reply["unavailable_sections"])
        self.assertNotIn(str(self.state), json.dumps(reply))
        self.assertNotIn("private details", json.dumps(reply))
        self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir()}, before)

    def test_stable_bounded_paging_with_colliding_timestamps(self):
        Supervisor(self.state, None, None, None)
        with closing(sqlite3.connect(self.state / "supervision.sqlite")) as db, db:
            db.executemany("INSERT INTO events(at,thread,event) VALUES('2026-01-01','t',?)",
                           [("context_clear",)] * 41)
        one = read_diagnostics(self.state, {})
        self.assertEqual(len(one["records"]), 10)
        self.assertTrue(one["has_more"])
        all_rows = list(one["records"])
        while one["has_more"]:
            one = read_diagnostics(self.state, {"before": one["next_before"], "limit": 30})
            all_rows.extend(one["records"])
        self.assertEqual([r["ref"] for r in all_rows], [f"screen:{i}" for i in range(41, 0, -1)])

    def test_chat_outcome_is_logged_when_turn_finishes_after_visible_block(self):
        now = datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)
        store = ChatStore(self.state, clock=lambda: now)
        store.submit("Show progress")
        batch = store.claim().id
        receipt = store.append_reply_block(batch, "reply-1", "Progress")
        self.assertEqual(read_diagnostics(self.state, {})["records"], [])
        now += timedelta(minutes=2)
        store.complete(batch, "")
        record, = read_diagnostics(self.state, {})["records"]
        self.assertEqual((record["event_id"], record["at_utc"]), (receipt, "2026-01-02T00:01:00.000Z"))

    def test_older_chat_outcome_uses_event_timestamp_before_and_after_upgrade(self):
        self.sql("chat.sqlite", "CREATE TABLE batches(id INTEGER PRIMARY KEY,status TEXT,outcome INTEGER)")
        self.sql("chat.sqlite", "CREATE TABLE events(id INTEGER PRIMARY KEY,at TEXT,text TEXT)")
        self.sql("chat.sqlite", "INSERT INTO events VALUES(7,'2026-01-01T23:59:00Z','historical reply')")
        self.sql("chat.sqlite", "INSERT INTO batches VALUES(1,'complete',7)")
        for upgraded in (False, True):
            if upgraded:
                self.sql("chat.sqlite", "ALTER TABLE batches ADD COLUMN finished_at TEXT")
            with self.subTest(upgraded=upgraded):
                record, = read_diagnostics(self.state, {})["records"]
                self.assertEqual((record["event_id"], record["at_utc"]), (7, "2026-01-01T23:59:00.000Z"))

    def test_reviews_identify_tools_without_showing_reviewed_arguments(self):
        Supervisor(self.state, None, None, None)
        for tool in ("view_pdf_page", "workspace_append", "workspace_replace"):
            self.sql("supervision.sqlite", "INSERT INTO events(thread,event,tool,call_id) VALUES(?,?,?,?)",
                     ("t", "tool_clear", tool, tool))
            self.sql("supervision.sqlite", "INSERT INTO actions VALUES(?,?,?,?)",
                     ("t", tool, "digest", json.dumps({"path": "/work/private-filename.pdf"})))
        report = read_diagnostics(self.state, {})
        self.assertCountEqual([(r["status"], r["tool"]) for r in report["records"]],
                              [("tool_clear", tool) for tool in ("view_pdf_page", "workspace_append", "workspace_replace")])
        self.assertNotIn("private-filename", json.dumps(report))

    def test_real_ledgers_show_only_allowlisted_results_and_receipts(self):
        secret = "SECRET_private_material_not_for_a_tool"
        store = ChatStore(self.state)
        store.submit(secret)
        first = store.claim().id
        receipt = store.fail(first, "The provider reached the 8,192-token response limit.")
        store.submit(secret)
        second = store.claim().id
        store.complete(second, secret)
        budget = Budget(self.state / "usage.sqlite", Decimal("0.2"))
        rid = budget.reserve({"provider": {"sort": "price"}}, thread=secret, batch_id=first, trigger="operator")
        budget.finish(rid, Decimal("0.03"), status="incomplete", evidence={"output_tokens": 8192},
                      ms=3655, charge_source="openrouter")
        Supervisor(self.state, None, None, None)
        ReviewerVendors(self.state, Decimal(5))
        for call_id, event, result in [
            ("done", "tool_clear", {"exit_code": 7, "output": secret, "timed_out": False, "truncated": False}),
            ("timeout", "tool_clear", {"exit_code": None, "output": secret, "timed_out": True, "truncated": False}),
            ("large", "opus_allowed", {"exit_code": None, "output": secret, "timed_out": False, "truncated": True}),
            ("runner", "tool_clear", {"error": "The coding runner is unavailable. The command was not sent."}),
            ("opus", "tool_review_unavailable", {"error": "Opus review unavailable (insufficient daily supervisor "
                                                 "allowance). The proposed tool action was held."}),
            ("denied", "opus_denied", {"error": "Claude 3 Opus denied this action.", "judgment": secret}),
        ]:
            self.sql("supervision.sqlite", "INSERT INTO events(thread,event,tool,call_id) VALUES(?,?,?,?)",
                     (secret, event, "coding_run", call_id))
            self.sql("supervision.sqlite", "INSERT INTO actions VALUES(?,?,?,?)",
                     (secret, call_id, secret, json.dumps(result)))
            self.sql("review-costs.sqlite", """INSERT INTO calls(id,at,day,vendor,purpose,reserved,cost,ms,
                status,thread_id,batch_id,tool,tool_call_id) VALUES(?,datetime('now'),'today','opus','escalation',
                100,1,22,'metered',?,?,'coding_run',?)""", (secret + call_id, secret, first, call_id))
        self.sql("review-costs.sqlite", """INSERT INTO opus_responses VALUES(?, 200,'complete',100,?,
                 NULL,'malformed_xml')""", (secret + "opus", secret))
        self.sql("supervision.sqlite", """INSERT INTO luna_flags(thread,batch_id,sequence,kind,tool,evidence,summary)
            VALUES(?,?,12,'context',?,?,?)""", (secret, first, secret, "credentials " + secret, secret))
        self.sql("supervision.sqlite", "INSERT INTO events(thread,event,tool,call_id) VALUES(?,?,?,?)",
                 (secret, secret, secret, secret))
        reply = read_diagnostics(self.state, {"batch_id": first, "limit": 30})
        self.assertEqual(reply["unavailable_sections"], [])
        rows = reply["records"]
        self.assertTrue(all(r["batch_id"] == first for r in rows))
        chat, = (r for r in rows if r["type"] == "chat")
        self.assertEqual((chat["event_id"], chat["outcome_code"]), (receipt, "provider_output_limit"))
        qwen, = (r for r in rows if r["type"] == "qwen")
        self.assertEqual((qwen["ref"], qwen["status"], qwen["duration_ms"], qwen["settled_usd"]),
                         ("qwen:" + rid, "incomplete", 3655, "0.03"))
        self.assertEqual(qwen["route"], "cheapest")
        self.assertTrue(any(r.get("validation") == "malformed_xml" and r["http_status"] == 200 for r in rows))
        self.assertTrue(any(r.get("evidence_topics") == ["credentials"] for r in rows))
        self.assertTrue(any(r.get("outcome_code") == "opus_budget_unavailable" for r in rows))
        self.assertTrue(any(r.get("outcome_code") == "opus_denied" for r in rows))
        results = [r["coding"] for r in rows if "coding" in r]
        self.assertTrue(any(c.get("exit_code") == 7 for c in results))
        self.assertTrue(any(c.get("timed_out") for c in results))
        self.assertTrue(any(c.get("truncated") for c in results))
        self.assertTrue(any(c.get("state") == "runner_unavailable" for c in results))
        self.assertNotIn(secret, json.dumps(read_diagnostics(self.state, {"limit": 30})))
        self.assertEqual(len(read_diagnostics(self.state, {"batch_id": second})["records"]), 1)
