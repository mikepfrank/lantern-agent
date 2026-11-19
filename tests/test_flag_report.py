from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.flag_report import flag_report
from lantern.supervision import DATABASE, Supervisor


class FlagReportTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.state / DATABASE

    def test_missing_state_and_legacy_database_are_read_only(self):
        self.assertEqual(flag_report(self.state)["flags"], [])
        self.assertFalse(self.path.exists())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE events(event TEXT)")
            db.execute("INSERT INTO events VALUES('tool_flagged')")
        before = self.path.read_bytes()
        report = flag_report(self.state)
        self.assertEqual(report["flags"], [])
        self.assertIn("from the introduction", report["coverage"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_filters_order_explanation_and_consistent_paging(self):
        Supervisor(self.state, None, None, None)
        evidence = "First line.\nExact reported evidence — second line."
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("""INSERT INTO luna_flags(at, thread, sequence, batch_id, kind, tool, call_id,
                                                     evidence, summary) VALUES (?,?,?,?,?,?,?,?,?)""", [
                ("2026-11-19T17:00:00Z", "a", 1, 10, "context", None, None, "Context concern", "Overview."),
                ("2026-11-19T17:00:00Z", "a", 2, 10, "tool", "heartbeat", "h", evidence, "Reason."),
                ("2026-11-19T17:00:00Z", "a", 2, 10, "unregistered_tool", "unregistered", None, "Other", "Blocked."),
                ("2026-11-19T17:01:00Z", "b", 1, None, "tool", "workspace_write", "w", "Other", "Held."),
            ])
        self.assertEqual([f["id"] for f in flag_report(self.state)["flags"]], [4, 3, 2, 1])
        first = flag_report(self.state, kind="tool", batch=10, limit=1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["flags"][0]["kind"], "unregistered_tool")
        self.assertEqual(first["next_before_id"], 3)
        next_page = flag_report(self.state, kind="tool", batch=10, limit=1, before_id=first["next_before_id"])
        self.assertFalse(next_page["has_more"])
        self.assertIsNone(next_page["next_before_id"])
        self.assertEqual(next_page["flags"], [
            {"id": 2, "at_utc": "2026-11-19T17:00:00Z", "thread_id": "a", "batch_id": 10, "request_sequence": 2,
             "kind": "tool", "tool": "heartbeat", "call_id": "h", "evidence": evidence, "summary": "Reason."}])
        self.assertEqual([f["id"] for f in flag_report(self.state, kind="context")["flags"]], [1])
        self.assertEqual(flag_report(self.state, batch=11)["flags"], [])

    def test_rejects_invalid_filters(self):
        for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 201}, {"kind": "unknown"},
                       {"batch": 0}, {"before_id": -2}, {"batch": 2**63}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                flag_report(self.state, **kwargs)
