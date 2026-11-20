from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from lantern import concerns


class ConcernStoreTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / "supervision.sqlite"
        self.db = self.enterContext(closing(sqlite3.connect(self.path)))
        with self.db:
            concerns.initialize(self.db)

    def create(self, *, sequence=1, evidence="One observed event", related=None):
        return concerns.create(self.db, "room", sequence, evidence, "Summary", related)

    def test_legacy_import_keeps_evidence_and_migrates_heard_rows_once(self):
        with self.db:
            self.db.execute("""CREATE TABLE findings (thread TEXT,sequence INTEGER,evidence TEXT,
                summary TEXT,heard INTEGER,PRIMARY KEY(thread,sequence))""")
            self.db.executemany("INSERT INTO findings VALUES ('room',?,?,?,?)",
                                [(1, "Previously heard", "First issue", 1), (2, "A new issue", "Second", 0)])
            concerns.initialize(self.db)
        one, two = concerns.list_open(self.db, "room")
        self.assertEqual([(r["status"], r["version"]) for r in (one, two)], [("pending", 0), ("pending", 0)])
        with self.db:
            self.assertTrue(concerns.record(self.db, "room", one["id"], 0, "retire", "Resolved first.", []))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            concerns.initialize(db)
            self.assertEqual([r["id"] for r in concerns.list_open(db, "room")], [two["id"]])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM findings").fetchone()[0], 2)
            db.execute("UPDATE findings SET evidence='Previously heard plus new facts' WHERE sequence=1")
            concerns.initialize(db)
            self.assertEqual(len(concerns.list_open(db, "room")), 2)
            self.assertEqual(len(concerns.history(db, "room")), 3)

    def test_scoped_watch_question_and_new_evidence_after_retirement(self):
        with self.db:
            original, other = self.create(), self.create(sequence=1, evidence="A separate event")
            self.assertNotEqual(original, other)
            self.assertTrue(concerns.record(self.db, "room", original, 0, "watch", "Review private writes.",
                                            ["workspace_write"]))
            self.assertTrue(concerns.record(self.db, "room", other, 0, "ask_operator", "Who owns the records?",
                                            ["*"]))
            self.assertEqual([r["id"] for r in concerns.list_open(self.db, "room", "heartbeat")], [other])
            self.assertEqual([r["id"] for r in concerns.list_open(self.db, "room", "workspace_write")],
                             [original, other])
            self.assertTrue(concerns.record(self.db, "room", original, 1, "retire", "Operator confirmed.", []))
            self.assertFalse(concerns.record(self.db, "room", original, 2, "watch", "A later attempt.", ["*"]))
            fresh = self.create(sequence=4, evidence="Another attempt after the ruling", related=original)
            with self.assertRaises(ValueError):
                concerns.create(self.db, "other", 4, "Different thread", "No shared access", original)
            self.assertEqual(concerns.list_open(self.db, "other"), [])
            self.assertEqual(concerns.history(self.db, "other"), [])
            self.assertFalse(concerns.record(self.db, "other", other, 1, "retire", "Foreign incident.", []))
            pending = concerns.list_open(self.db, "room", "heartbeat")
            self.assertEqual([(r["id"], r["status"]) for r in pending], [(other, "question"), (fresh, "pending")])
            self.assertEqual(pending[-1]["related_id"], original)
        rulings = self.db.execute("SELECT version,status,reason FROM concern_rulings WHERE concern_id=? ORDER BY version",
                                  (original,)).fetchall()
        self.assertEqual(rulings, [(1, "watch", "Review private writes."), (2, "retired", "Operator confirmed.")])

    def test_compact_history_does_not_clip_open_finding_for_adjudication(self):
        text = "start-" + "x" * 900 + "-end of evidence"
        with self.db:
            for sequence in range(1, 19):
                self.create(sequence=sequence, evidence=text)
        recent = concerns.history(self.db, "room")
        self.assertEqual(len(recent), 16)
        self.assertEqual(recent[0]["sequence"], 18)
        self.assertLessEqual(len(recent[0]["evidence"]), 600)
        self.assertTrue(recent[0]["evidence"].endswith("-end of evidence"))
        active = concerns.list_open(self.db, "room", "workspace_write")
        self.assertEqual(len(active), 18)
        self.assertEqual(active[0]["evidence"], text)

    def test_repeat_retains_the_exact_ruling_across_scans_without_reopening_it(self):
        with self.db:
            old = self.create()
            concerns.record(self.db, "room", old, 0, "retire", "The past event was resolved.", [])
            newer = self.create(sequence=2)
            self.assertEqual(concerns.history(self.db, "room", 1)[0]["id"], newer)
            self.assertTrue(concerns.repeat_seen(self.db, "room", 3, old))
            self.assertFalse(concerns.repeat_seen(self.db, "room", 3, old))
            with self.assertRaises(ValueError):
                concerns.repeat_seen(self.db, "room", 3, newer)
            with self.assertRaises(ValueError):
                concerns.repeat_seen(self.db, "other", 4, old)
        self.assertIsNone(concerns.get(self.db, "other", old))
        recent, = concerns.history(self.db, "room", 1)
        self.assertEqual(recent, concerns.get(self.db, "room", old))
        self.assertEqual((recent["id"], recent["version"], recent["status"]), (old, 1, "retired"))
        self.assertEqual(self.db.execute("SELECT thread,sequence,concern_id FROM concern_repeats").fetchall(),
                         [("room", 3, old)])

    def test_compare_and_append_ruling_across_connections(self):
        with self.db:
            cid = self.create()
        ready = Barrier(2)
        def adjudicate(reason):
            with closing(sqlite3.connect(self.path)) as db, db:
                ready.wait(timeout=5)
                return concerns.record(db, "room", cid, 0, "watch", reason, ["workspace_write"])
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(adjudicate, ("One ruling", "A concurrent ruling")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(concerns.list_open(self.db, "room")[0]["version"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM concern_rulings").fetchone()[0], 1)

    def test_invalid_ruling_cannot_widen_or_drop_scope(self):
        with self.db:
            cid = self.create()
            for decision, tools in (("retire", ["*"]), ("watch", []), ("ask_operator", []),
                                    ("watch", ["*", "coding_run"]), ("watch", ["coding_run", "coding_run"]),
                                    ("watch", ["coding/run"]), ("retire", None), ("allow", [])):
                with self.subTest(decision=decision, tools=tools), self.assertRaises(ValueError):
                    concerns.record(self.db, "room", cid, 0, decision, "Reason", tools)
        self.assertEqual(concerns.list_open(self.db, "room")[0]["status"], "pending")
