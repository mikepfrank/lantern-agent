import asyncio
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from lantern.codex_session import _Peer, run_turn
from lantern.history import HISTORY_TOOLS
from lantern.pdf_tool import PDF_TOOL
from lantern.thread_upgrade import _migrate, install_room_tools
from lantern.transport import MODEL, OpenRouterGateway
from test_codex_smoke import reply


def saved(tool):
    return {"type": "function", **tool}


class CompactionRegistryTests(unittest.TestCase):
    def test_interrupted_checkpoint_repair_takes_its_own_snapshot_and_preserves_later_turns(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp).resolve()
            home = state / "codex"
            (home / "sessions").mkdir(parents=True)
            tid = str(uuid4())
            rollout = home / "sessions" / f"rollout-{tid}.jsonl"
            old = {"name": "chess_analyze", "description": "Chess", "inputSchema": {"type": "object"}}
            def line(kind, payload):
                return (json.dumps({"type": kind, "payload": payload}) + "\n").encode()
            def checkpoint(owner, tools):
                return line("compacted", {"resume_metadata": {"dynamic_tools": {"thread_id": owner, "tools": tools}},
                                          "replacement_history": [{"type": "message", "role": "assistant"}]})
            baseline = [line("session_meta", {"id": tid, "dynamic_tools": [saved(old)]}),
                        checkpoint(tid, [saved(old)]), checkpoint("some-other-thread", []),
                        line("compacted", {"resume_metadata": {}}), checkpoint(tid, []),
                        line("event_msg", {"text": "recent operator exchange"})]
            rollout.write_bytes(b"".join(baseline))
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads(id, rollout_path)")
                db.execute("INSERT INTO threads VALUES(?,?)", (tid, str(rollout)))
            with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db, db:
                db.execute("CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,rollout_end_ordinal,"
                           "rollout_byte_offset,rollout_end_byte_offset)")
                db.execute("CREATE TABLE thread_history_projection_state(thread_id,next_rollout_ordinal,next_rollout_byte_offset)")
                db.execute("INSERT INTO thread_turns VALUES(?,?,5,5,?,?)",
                           (tid, "t", len(b"".join(baseline[:-1])), rollout.stat().st_size))
                db.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (tid, 6, rollout.stat().st_size))
            # A deployed v1 update already added the tool to the opening record.
            _migrate(home, tid, [PDF_TOOL], state / "operator-backups/pdf-pages-v1", "pdf-pages")
            old_backup, = (state / "operator-backups/pdf-pages-v1").glob("*.jsonl")
            next_line = line("event_msg", {"text": "request received after deployment"})
            with rollout.open("ab") as stream:
                stream.write(next_line)
            before = rollout.read_bytes()
            replace = os.replace
            def interrupt(src, dst):
                replace(src, dst)
                if Path(dst) == rollout:
                    raise RuntimeError("interrupted")
            with patch("lantern.thread_upgrade.os.replace", side_effect=interrupt), self.assertRaises(RuntimeError):
                install_room_tools(state, tid, [old, PDF_TOOL])
            install_room_tools(state, tid, [old, PDF_TOOL])
            repair, = (state / "operator-backups/room-tools-v2").glob("*.jsonl")
            self.assertEqual(old_backup.read_bytes(), b"".join(baseline))
            self.assertEqual(repair.read_bytes(), before)
            self.assertTrue(repair.with_suffix(".done").exists())
            after = rollout.read_bytes().splitlines(keepends=True)
            for i in (1, 4):
                self.assertEqual(json.loads(after[i])["payload"]["resume_metadata"]["dynamic_tools"]["tools"],
                                 [saved(old), saved(PDF_TOOL)])
            self.assertEqual(after[2:4], baseline[2:4])
            self.assertEqual(after[5:], [baseline[5], next_line])
            with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db:
                self.assertEqual(db.execute("SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns").fetchone(),
                                 (len(b"".join(after[:5])), len(b"".join(after[:6]))))
            # Another release may add a tool after the original snapshot was completed.
            extra = {**old, "name": "harness_logs"}
            install_room_tools(state, tid, [old, PDF_TOOL, extra])
            self.assertIsNone(install_room_tools(state, tid, [old, PDF_TOOL, extra]))
            self.assertEqual(repair.read_bytes(), before)
            self.assertEqual(len(list(repair.parent.glob("*.jsonl"))), 2)
            # Interrupt a definition change with simultaneous removal. Restart
            # with a third configuration; replay the frozen target first.
            revised = {**old, "description": "Updated chess tool", "inputSchema": {"type": "object", "required": ["fen"]}}
            with patch("lantern.thread_upgrade.os.replace", side_effect=interrupt), self.assertRaises(RuntimeError):
                install_room_tools(state, tid, [revised])
            interrupted = rollout.read_bytes()
            self.assertEqual(json.loads(interrupted.splitlines()[0])["payload"]["dynamic_tools"], [saved(revised)])
            install_room_tools(state, tid, [PDF_TOOL, revised])
            final = rollout.read_bytes().splitlines(keepends=True)
            for i in (1, 4):
                self.assertEqual(json.loads(final[i])["payload"]["resume_metadata"]["dynamic_tools"]["tools"],
                                 [saved(PDF_TOOL), saved(revised)])
            self.assertEqual(final[2:4], baseline[2:4])
            self.assertEqual(final[5:], [baseline[5], next_line])
            self.assertIsNone(install_room_tools(state, tid, [PDF_TOOL, revised]))
            install_room_tools(state, tid, [])
            cleared = [json.loads(line)["payload"] for line in rollout.read_bytes().splitlines()]
            self.assertEqual(cleared[0]["dynamic_tools"], [])
            self.assertEqual(cleared[1]["resume_metadata"]["dynamic_tools"]["tools"], [])

    @unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
    def test_native_manual_compaction_then_migrated_resume_advertises_tool(self):
        old = {"name": "chess_analyze", "description": "Chess", "inputSchema": {"type": "object"}}
        calls = []
        @contextmanager
        def provider(body, headers):
            calls.append(json.loads(body))
            n = len(calls)
            yield reply(n, {"type": "message", "role": "assistant", "id": f"msg_{n}", "status": "completed",
                            "content": [{"type": "output_text", "text": "Synthetic chess note."}]})
        finish = _Peer.finish_turn
        async def compact(peer, *args):
            await finish(peer, *args)
            peer.turn_id, peer.finished = None, False
            await peer.request("thread/compact/start", {"threadId": peer.thread_id})
            await finish(peer, *args)
        with TemporaryDirectory() as tmp, patch("lantern.transport._open_upstream", provider):
            state = Path(tmp).resolve()
            home, work = state / "codex", state / "work"
            home.mkdir()
            work.mkdir()
            def run(tools, tid=None):
                with OpenRouterGateway("fixture", route="fastest", tools=tools, reserve=lambda _: 1,
                                       settle=lambda *_: None, observe=lambda *_: None, unregistered=lambda *_: None,
                                       resuming=bool(tid)) as gate:
                    return asyncio.run(run_turn(codex_bin=os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex"),
                                                codex_home=home, workspace=work, model=MODEL, provider="lantern",
                                                persona="Test.", prompt="Recall the chess note.", tools=tools,
                                                dispatch=lambda *_: {}, thread_id=tid, base_url=gate.base_url,
                                                env={"LANTERN_GATEWAY_TOKEN": gate.token}, timeout=28))
            with patch.object(_Peer, "finish_turn", compact):
                tid = run([old]).thread_id
            self.assertTrue(any(not p["tools"] for p in calls))
            _migrate(home, tid, [PDF_TOOL], state / "operator-backups/pdf-pages-v1", "pdf-pages")
            upgraded = [old, PDF_TOOL, *HISTORY_TOOLS]
            report = install_room_tools(state, tid, upgraded)
            self.assertIsNotNone(report)
            self.assertEqual(run(upgraded, tid).thread_id, tid)
            self.assertEqual({t["name"] for t in calls[-1]["tools"]},
                             {"chess_analyze", "view_pdf_page", "history_search", "history_open"})
            revised = {**old, "description": "A revised chess tool.", "inputSchema": {"type": "object", "required": ["fen"]}}
            self.assertIsNotNone(install_room_tools(state, tid, [revised]))
            self.assertEqual(run([revised], tid).thread_id, tid)
            self.assertEqual([(t["name"], t["parameters"]) for t in calls[-1]["tools"]],
                             [("chess_analyze", revised["inputSchema"])])
