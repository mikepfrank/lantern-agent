from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.chat_store import ChatStore
from lantern.compaction_annotations import annotation_text, import_compaction_annotations


def stamp(second):
    return datetime(2026, 11, 27, 18, 0, second, tzinfo=timezone.utc)


def record(kind, at, payload):
    return (json.dumps({"timestamp": at.isoformat(), "type": kind, "payload": payload}) + "\n").encode()


def compact(second, window):
    return record("compacted", stamp(second), {"window_id": window, "replacement_history": [
        {"role": "user", "content": "private text, never include in an annotation"}]})


class CompactionAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.store = ChatStore(self.state, clock=lambda: stamp(2))
        self.first = self.store.submit("original request")
        batch = self.store.claim()
        self.tid = "private-room"
        self.store.bind_thread(batch.id, self.tid)
        self.store.clock = lambda: stamp(7)
        self.last = self.store.complete(batch.id, "original reply")
        self.home = self.state / "codex"
        (self.home / "sessions").mkdir(parents=True)
        self.rollout = self.home / "sessions" / "rollout-private-room.jsonl"
        self.header = record("session_meta", stamp(0), {"id": self.tid})
        self.rollout.write_bytes(self.header + compact(1, "one") + compact(5, "two") + compact(7, "three"))
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id,rollout_path)")
            db.execute("INSERT INTO threads VALUES(?,?)", (self.tid, str(self.rollout)))

    def test_preview_does_not_modify_room_and_apply_preserves_history(self):
        original = self.store.path.read_bytes()
        events = self.store.events()
        check = import_compaction_annotations(self.state, now=stamp(8))
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual([p["after_event_id"] for p in check["placements"]], [None, self.first, self.last])
        self.assertEqual((check["checkpoints"], check["added"], check["to_add"]), (3, 0, 3))
        applied = import_compaction_annotations(self.state, apply=True, now=stamp(8))
        self.assertEqual((applied["added"], self.store.events()), (3, events))
        self.assertEqual(self.store.status()["queued"], 0)
        with closing(sqlite3.connect(self.store.path)) as db:
            rows = db.execute("SELECT * FROM compaction_annotations ORDER BY id").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertNotIn("private text", repr(rows))
        self.assertTrue(all(row[3] == stamp(8).isoformat(timespec="microseconds") for row in rows))
        self.assertIn("was not shown", annotation_text(rows[0][2]))
        self.assertIn("12:00:01 PM CST", annotation_text(rows[0][2]))
        with self.rollout.open("ab") as f:
            f.write(compact(8, "at-fence") + compact(9, "later"))
        again = import_compaction_annotations(self.state, apply=True, now=stamp(15))
        self.assertEqual(again["before"], applied["before"])
        self.assertEqual((again["checkpoints"], again["added"]), (3, 0))

    def test_existing_live_notices_bound_the_first_apply_by_batch_start(self):
        self.store.clock = lambda: stamp(8)
        self.store.submit("live turn")
        batch = self.store.claim()
        self.store.record_compaction(batch.id, self.tid, "rpc-id", "completed", int(stamp(10).timestamp() * 1000))
        with self.rollout.open("ab") as f:
            f.write(compact(9, "not-rpc-id"))
        for apply in (False, True, True):
            result = import_compaction_annotations(self.state, apply=apply, before=stamp(12), now=stamp(13))
            self.assertEqual((result["before"], result["checkpoints"]), (stamp(8).isoformat(timespec="microseconds"), 3))

    def test_duplicate_checkpoint_different_windows_and_nested_copies(self):
        self.rollout.write_bytes(self.header + compact(2, "one") + record("response_item", stamp(2),
            {"type": "message", "payload": {"type": "compacted"}}) + compact(2, "one") + compact(2, "two"))
        result = import_compaction_annotations(self.state, apply=True, now=stamp(6))
        self.assertEqual(result["added"], 2)
        with self.rollout.open("ab") as f:
            f.write(compact(4, "one"))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            import_compaction_annotations(self.state, apply=True, now=stamp(8))

    def test_bad_binding_and_timestamps_reject_before_creating_annotations(self):
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            import_compaction_annotations(self.state, before="2026-11-26", apply=True, now=stamp(8))
        with self.assertRaisesRegex(ValueError, "past or present"):
            import_compaction_annotations(self.state, before=stamp(9), apply=True, now=stamp(8))
        self.rollout.write_bytes(record("session_meta", stamp(0), {"id": "another-room"}))
        with self.assertRaisesRegex(ValueError, "different thread"):
            import_compaction_annotations(self.state, apply=True, now=stamp(8))
        with self.store._connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='compaction_annotations'").fetchone())
