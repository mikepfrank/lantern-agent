from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.chat_store import ChatStore
from lantern.compaction_annotations import ANNOTATIONS_SCHEMA
from lantern.history import PAGE, history_tool
from lantern.supervision import Supervisor
from test_history_calls import call, record, result


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.store = ChatStore(self.state, clock=lambda: datetime(2026, 11, 27, 5, 59, tzinfo=timezone.utc))
        self.first = self.store.submit("Exact chess_%.α value " + "A" * (PAGE + 50) + " final anchor")
        self.turn = self.store.claim().id
        self.thread = "room-one"
        self.store.bind_thread(self.turn, self.thread)
        self.reply = self.store.complete(self.turn, "reply from Lantern")
        codex = self.state / "codex"
        (codex / "sessions").mkdir(parents=True)
        self.rollout = codex / "sessions" / "rollout-room-one.jsonl"
        self.header = record("session_meta", {"id": self.thread})
        self.rollout.write_bytes(self.header)
        with closing(sqlite3.connect(codex / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id,rollout_path)")
            db.execute("INSERT INTO threads VALUES (?,?)", (self.thread, str(self.rollout)))

    def tool(self, name, args, **kwargs):
        value = history_tool(self.state, self.thread, name, args, **kwargs)
        self.assertNotIn("error", value, value)
        return value

    def test_literal_open_pages_neighbors_search_cursor_and_central_dates(self):
        self.assertFalse(self.tool("history_search", {"query": "chessX%"})["matches"])
        match, = self.tool("history_search", {"query": "final anchor", "source": "operator",
            "since": "2026-11-26", "until": "2026-11-26", "turn": self.turn})["matches"]
        self.assertEqual(match["ref"], f"room:{self.first}")
        page = self.tool("history_open", {"ref": match["ref"]})
        self.assertEqual(page["item"]["next_offset"], PAGE)
        self.assertEqual(page["after"][0]["ref"], f"room:{self.reply}")
        tail = self.tool("history_open", {"ref": match["ref"], "offset": page["item"]["next_offset"]})
        self.assertEqual(page["item"]["text"] + tail["item"]["text"], self.store.events()[0].text)
        self.assertIn("final anchor", self.tool("history_open", {"ref": match["ref"], "offset": match["offset"]})["item"]["text"])
        self.assertFalse(self.tool("history_search", {"query": "chess_%.α", "since": "2026-11-27"})["matches"])
        stamp = "2026-11-26T23:59:00-06:00"
        self.assertEqual(self.tool("history_search", {"author": "operator", "since": stamp, "until": stamp})
                         ["matches"][0]["ref"], match["ref"])
        self.assertEqual(self.tool("history_search", {"query": "VALUE Exact", "mode": "words"})["matches"][0]["ref"], match["ref"])
        self.assertEqual(self.tool("history_search", {"query": "Α", "mode": "words"})["matches"][0]["ref"], match["ref"])
        first_page = self.tool("history_search", {"turn": self.turn, "limit": 1})
        self.assertEqual(first_page["matches"][0]["ref"], f"room:{self.reply}")
        older = self.tool("history_search", {"turn": self.turn, "limit": 1, "cursor": first_page["next_cursor"]})
        self.assertEqual(older["matches"][0]["ref"], match["ref"])
        self.assertIsNone(older["next_cursor"])

    def test_source_scope_as_of_and_unadmitted_input(self):
        self.store.submit("unadmitted input")
        self.assertFalse(self.tool("history_search", {"query": "unadmitted"})["matches"])
        self.assertIn("error", history_tool(self.state, "other", "history_open", {"ref": "room:1"}))
        self.assertFalse(self.tool("history_search", {"source": "operator"}, cutoff="2026-11-27T05:00:00Z")["matches"])
        self.assertIn("error", history_tool(self.state, self.thread, "history_open", {"ref": "room:1"},
                                            cutoff="2026-11-27T05:00:00Z"))
        for args in ({"query": "x", "since": "yesterday"}, {"query": "x", "limit": True}, {"cutoff": "2099"}):
            self.assertIn("error", history_tool(self.state, self.thread, "history_search", args))

    def test_compaction_service_event_is_discoverable_by_its_room_turn(self):
        self.store.submit("Continue.")
        batch = self.store.claim()
        self.store.record_compaction(batch.id, self.thread, "compact-1", "started")
        self.assertFalse(self.tool("history_search", {"source": "service", "turn": batch.id})["matches"])
        event_id = self.store.record_compaction(batch.id, self.thread, "compact-1", "completed")
        match, = self.tool("history_search", {"query": "compaction", "source": "service", "turn": batch.id})["matches"]
        self.assertEqual(match["ref"], f"room:{event_id}")
        item = self.tool("history_open", {"ref": match["ref"]})["item"]
        event = self.store.latest_compaction(self.thread)
        self.assertEqual((item["text"], item["author"]), (event.text, "service"))
        self.assertEqual(datetime.fromisoformat(item["at"]), datetime.fromisoformat(event.at))
        self.assertEqual(self.store.status()["queued"], 0)

    def test_retrospective_compactions_preserve_refs_chronology_and_as_of_evidence(self):
        occurred = "2026-11-27T06:00:00.000000+00:00"
        added = "2026-11-28T17:12:34.000000+00:00"
        with self.store._connect(write=True) as db:
            db.execute(ANNOTATIONS_SCHEMA)
            db.executemany("""INSERT INTO compaction_annotations(id,thread_id,occurred_at,recorded_at,
                after_event_id,checkpoint_key) VALUES(?,?,?,?,?,?)""", [
                (i, thread, occurred, added, self.reply, f"internal-key-{i}")
                for i, thread in [(1, self.thread), (2, self.thread), (3, "other-room")]])
        search = {"source": "service", "query": "compaction", "limit": 1}
        newest = self.tool("history_search", search)
        self.assertEqual(newest["next_cursor"], "annotation:2")
        older = self.tool("history_search", {**search, "cursor": newest["next_cursor"]})
        self.assertEqual(older["matches"][0]["ref"], "annotation:1")
        self.assertIsNone(older["next_cursor"])
        page = self.tool("history_open", {"ref": "annotation:1"})
        item = page["item"]
        self.assertEqual((item["at"], item["recorded_at"]), (occurred, added))
        self.assertEqual((item["source"], item["author"], item["provenance"]),
                         ("service", "service", "retrospective_checkpoint_annotation"))
        self.assertIn("checkpoint", item["text"])
        self.assertNotIn("internal-key", repr(page))
        self.assertEqual(page["before"][0]["ref"], f"room:{self.reply}")
        self.assertEqual(page["after"][0]["ref"], "annotation:2")
        self.assertIn("error", history_tool(self.state, self.thread, "history_open", {"ref": "annotation:3"}))
        before_added = "2026-11-28T17:12:33Z"
        self.assertFalse(self.tool("history_search", {**search, "until": before_added})["matches"])
        self.assertFalse(self.tool("history_search", {**search, "until": added}, cutoff=before_added)["matches"])
        self.assertIn("error", history_tool(self.state, self.thread, "history_open", {"ref": "annotation:1"},
                                            cutoff=before_added))
        replay = self.tool("history_open", {"ref": f"room:{self.reply}"}, cutoff=before_added)
        self.assertEqual(replay["after"], [])
        self.assertEqual(self.tool("history_search", {**search, "until": added}, cutoff=added)["next_cursor"],
                         "annotation:2")

    def test_native_records_pair_and_survive_compaction_concurrent_reads_and_restart(self):
        with self.rollout.open("ab") as file:
            file.write(record("event_msg", {"type": "task_started", "turn_id": "native-id"}))
            file.write(call("move", "chess_analyze", '{"fen":"P14BP"}'))
        self.assertEqual(self.tool("history_open", {"ref": "call:move"})["item"]["related"], [])
        with self.rollout.open("ab") as file:
            file.write(result("move", '{"fen":"P5BP"}'))
            file.write(record("response_item", {"type":"message","role":"assistant","id":"delivered","content":[]}))
            file.write(record("compacted", {"replacement_history": [{"text": "spurious P15BP"}]}))
        with self.store._connect(write=True) as db:
            db.execute("INSERT INTO reply_blocks VALUES(?,?,?)", (self.turn, "delivered", self.reply))
        with ThreadPoolExecutor(max_workers=3) as pool:
            lookups = list(pool.map(lambda _: self.tool("history_search", {"tool": "chess_analyze", "turn": self.turn}), range(5)))
        for lookup in lookups:
            self.assertEqual([r["ref"] for r in lookup["matches"]], ["result:move", "call:move"])
        item = self.tool("history_open", {"ref": "result:move"})["item"]
        self.assertEqual(item["text"], '{"fen":"P5BP"}')
        self.assertEqual(item["related"], ["call:move"])
        self.assertFalse(self.tool("history_search", {"query": "P15BP"})["matches"])
        self.rollout.unlink()
        degraded = self.tool("history_search", {"query": "P5BP"})
        self.assertFalse(degraded["matches"])
        self.assertTrue(degraded["warnings"])

    def test_dated_ruling_scope_and_release_authorship(self):
        Supervisor(self.state, None, None, None)
        with closing(sqlite3.connect(self.state / "supervision.sqlite")) as db, db:
            db.execute("""INSERT INTO context_concerns(id,thread,sequence,evidence,summary)
                VALUES(1,?,1,'private screener text','topic')""", (self.thread,))
            db.execute("""INSERT INTO concern_rulings VALUES(1,1,'watch','earlier reason','["workspace_append"]',
                '2026-11-26 07:00:00')""")
            db.execute("""INSERT INTO concern_rulings VALUES(1,2,'retired','Direct operator release from room event #1. Scope here',
                '[]','2026-11-27 07:00:00')""")
        with self.store._connect(write=True) as db:
            receipt = self.store._append(db, "system", "Mike retired context concern #1. See operator event #1.")
            db.execute("""INSERT INTO operator_concern_releases
                VALUES(?,1,'[]','Scope here','2026-11-27T07:00:01Z',1,NULL,?)""", (self.thread, receipt))
        ruling = self.tool("history_open", {"ref": "ruling:1:2"})["item"]
        self.assertEqual(ruling["author"], "operator")
        self.assertIn("retired", ruling["text"])
        self.assertIn("room:1", ruling["related"])
        self.assertIn("ruling:1:2", self.tool("history_open", {"ref": "room:1"})["item"]["related"])
        previous, = self.tool("history_search", {"source": "ruling"}, cutoff="2026-11-26T20:00:00Z")["matches"]
        self.assertEqual((previous["ref"], previous["author"]), ("ruling:1:1", "reviewer"))
        self.assertFalse(self.tool("history_search", {"query":"private screener text"})["matches"])
