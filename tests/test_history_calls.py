import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lantern.history_calls import call_event_stream


def record(kind, payload):
    return (json.dumps({"timestamp": "2026-11-27T12:46:30.004Z", "type": kind, "payload": payload})
            + "\n").encode()


def call(call_id, name, arguments):
    return record("response_item", {"type": "function_call", "name": name, "call_id": call_id,
                                    "arguments": arguments})


def result(call_id, output):
    return record("response_item", {"type": "function_call_output", "call_id": call_id, "output": output})


class NativeHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.tid = "room-id"
        (self.home / "sessions").mkdir()
        self.path = self.home / "sessions" / "rollout-room-id.jsonl"
        self.header = record("session_meta", {"id": self.tid})
        self.path.write_bytes(self.header)
        db = sqlite3.connect(self.home / "state_5.sqlite")
        try:
            db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?)", (self.tid, str(self.path)))
            db.commit()
        finally:
            db.close()
        self.names = {}

    def read(self, offset=0, turn=None):
        events = []
        with call_event_stream(self.home, self.tid) as stream:
            for offset, event in stream.read(offset, self.names.get, turn):
                if event:
                    events.append(event)
                    if event["source"] == "call":
                        self.names[event["call_id"]] = event["tool"]
            return offset, stream.native_turn, events

    def test_originals_only_pair_across_partial_append_and_restart(self):
        fen = '{"fen" : "R2QR2K1 w - 4 12", "rationale" : "analyze"}'
        receipt = result("one", '{"error": "Invalid tool arguments."}')
        first = (self.header + record("event_msg", {"type": "task_started", "turn_id": "native-9"})
                 + call("one", "chess_analyze", fen)
                 + record("compacted", {"replacement_history": [
                     {"type": "function_call", "call_id": "copy", "name": "chess_analyze", "arguments": fen}]})
                 + record("response_item", {"type": "reasoning", "summary": "private scratchpad"}))
        self.path.write_bytes(first + receipt[:20])
        offset, turn, rows = self.read()
        self.assertEqual((offset, turn), (len(first), "native-9"))
        self.assertEqual([(r["ref"], r["text"]) for r in rows], [("call:one", fen)])
        with self.path.open("ab") as f:
            f.write(receipt[20:] + record("response_item", {"type": "message", "role": "assistant",
                                                            "id": "reply-3", "content": []})
                    + record("event_msg", {"type": "task_complete", "turn_id": "native-9"}))
        _, turn, rows = self.read(offset, turn)
        self.assertIsNone(turn)
        self.assertEqual(rows[0]["ref"], "result:one")
        self.assertEqual(rows[0]["native_turn"], "native-9")
        self.assertEqual(rows[0]["text"], '{"error": "Invalid tool arguments."}')
        self.assertEqual(rows[1], {"source": "anchor", "native_turn": "native-9", "item_id": "reply-3"})

    def test_private_content_and_history_echoes_excluded(self):
        records = [
            call("h", "read_heritage", '{"filename":"book.pdf","rationale":"secret-source"}'),
            result("h", '{"quotation":"secret-source","path":"book.pdf","opus_review_text":"secret-source"}'),
            call("w", "workspace_append", '{"path":"journal","text":"secret-source","expected_suffix":"secret-source"}'),
            result("w", '{"error":"denied","judgment":"actual visible reason","opus_context_review":"secret-source"}'),
            call("p", "view_pdf_page", '{"path":"book.pdf","page":2}'),
            result("p", [{"type": "input_text", "text": '{"page_count":2}'},
                         {"type": "input_image", "image_url": "secret-source"}]),
            call("next", "history_open", '{"ref":"call:h"}'), result("next", '{"copy":"secret-source"}'),
            call("future", "new_unknown_tool", '{"input":"secret-source"}'),
            result("future", '{"output":"secret-source"}'), result("orphan", '{"value":"secret-source"}'),
        ]
        self.path.write_bytes(self.header + b"".join(records))
        _, _, events = self.read()
        self.assertEqual(len(events), 8)
        self.assertNotIn("secret-source", str(events))
        self.assertIn("Content omitted", events[0]["text"])
        self.assertIn("actual visible reason", events[3]["text"])
        self.assertIn("page_count", events[5]["text"])
        self.assertIn("Content omitted", events[5]["text"])

    def test_only_nested_review_side_channels_are_removed_from_grant_receipts(self):
        output = {"grants": [{"receipt": {"filename": "sky.csv", "sha256": "123",
                                          "opus_review_text": "not indexed"}}],
                  "opus_context_review": {"response_text": "not indexed"}}
        self.path.write_bytes(self.header + call("g", "file_import_status", "{}")
                              + result("g", json.dumps(output)))
        _, _, events = self.read()
        text = events[-1]["text"]
        self.assertIn("sky.csv", text)
        self.assertIn('"sha256": "123"', text)
        self.assertIn("Content omitted", text)
        self.assertNotIn("not indexed", text)

    def test_identity_check_and_derived_identity_changes_on_header_rewrite(self):
        with call_event_stream(self.home, self.tid) as stream:
            old_identity = stream.identity
        self.path.write_bytes(record("session_meta", {"id": self.tid, "dynamic_tools": []}))
        with call_event_stream(self.home, self.tid) as stream:
            self.assertNotEqual(old_identity, stream.identity)
        self.path.write_bytes(record("session_meta", {"id": "different-room"}))
        with self.assertRaisesRegex(ValueError, "different thread"):
            self.read()

    def test_corrupted_jsonl_fails_with_an_actionable_history_error(self):
        for line in (b"[]\n", b"null\n", b"{}\n", b'{"payload":[]}' + b"\n", b"!\n"):
            with self.subTest(line=line):
                self.path.write_bytes(self.header + line)
                with self.assertRaisesRegex(ValueError, "native history record"):
                    self.read()
