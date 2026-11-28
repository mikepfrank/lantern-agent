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

from lantern.chat_store import ChatStore
from lantern.codex_session import CodexError, _Peer, _isolation_args, run_turn
from lantern.history import HISTORY_TOOLS
from lantern.pdf_tool import PDF_TOOL
from lantern.thread_upgrade import _migrate, install_room_tools
from lantern.transport import MODEL, OpenRouterGateway
from test_codex_smoke import reply


def saved(tool):
    return {"type": "function", **tool}


class CompactionRegistryTests(unittest.TestCase):
    def test_compaction_notifications_are_scoped_and_duplicate_delivery_is_idempotent(self):
        seen = []
        peer = _Peer(None, [], None, on_compaction=lambda *event: seen.append(event))
        peer.thread_id, peer.turn_id = "thread", "turn"

        def notify(method, **params):
            asyncio.run(peer.handle({"method": method, "params": {
                "item": {"type": "contextCompaction", "id": "first"},
                "threadId": "thread", "turnId": "turn", **params}}))

        for wrong in ({"threadId": "other"}, {"turnId": "other"}, {"threadId": None},
                      {"turnId": None}, {"item": {"type": "contextCompaction"}}):
            with self.subTest(wrong=wrong), self.assertRaises(CodexError):
                notify("item/started", **wrong)
        notify("item/started", startedAtMs=10)
        notify("item/started", startedAtMs=11)
        notify("item/completed", completedAtMs=20)
        notify("item/completed", completedAtMs=21)
        notify("item/started", startedAtMs=10)
        notify("item/completed", item={"type": "contextCompaction", "id": "second"})
        self.assertEqual(seen, [("thread", "first", "started", 10), ("thread", "first", "completed", 20),
                                ("thread", "second", "completed", None)])

    @unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
    def test_automatic_compaction_updates_room_and_following_model_request(self):
        calls, activity = [], []
        tool = {"name": "chess_analyze", "description": "Chess", "inputSchema": {"type": "object"}}

        @contextmanager
        def provider(body, headers):
            calls.append(json.loads(body))
            n = len(calls)
            item = {"type": "message", "role": "assistant", "id": f"msg_{n}", "status": "completed",
                    "content": [{"type": "output_text", "text": "A synthetic chess note."}]}
            yield reply(n, item, input_tokens=5500 if n == 1 else 100)

        def small_context(*args):
            return [*_isolation_args(*args), "-c", "model_auto_compact_token_limit=4200"]

        with TemporaryDirectory() as tmp, patch("lantern.transport._open_upstream", provider), \
                patch("lantern.codex_session._isolation_args", small_context):
            state = Path(tmp).resolve()
            store = ChatStore(state)
            home, work = state / "codex", state / "work"
            home.mkdir()
            work.mkdir()
            for prompt in ("A chess note.", "Continue the note."):
                store.submit(prompt)
                batch = store.claim().id
                tid = store.status()["thread_id"]

                def record(thread_id, item_id, phase, at_ms):
                    store.record_compaction(batch, thread_id, item_id, phase, at_ms)
                    activity.append(store.status()["compacting"])

                with OpenRouterGateway("fixture", route="fastest", tools=[tool], reserve=lambda _: 1,
                        settle=lambda *_: None, observe=lambda *_: None, unregistered=lambda *_: None,
                        resuming=bool(tid), latest_compaction=lambda: store.latest_compaction(tid)) as gate:
                    result = asyncio.run(run_turn(codex_bin=os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex"),
                        codex_home=home, workspace=work, model=MODEL, provider="lantern", persona="Test.",
                        prompt=prompt, tools=[tool], dispatch=lambda *_: {}, thread_id=tid, base_url=gate.base_url,
                        on_thread=lambda thread_id: store.bind_thread(batch, thread_id), on_compaction=record,
                        env={"LANTERN_GATEWAY_TOKEN": gate.token}, timeout=28))
                store.complete(batch, result.text)

            self.assertEqual([bool(call["tools"]) for call in calls], [True, False, True])
            self.assertEqual(activity, [True, False])
            notice, = [event for event in store.events() if event.role == "system"]
            self.assertIn("Context compaction completed", notice.text)
            self.assertIn(notice.text, json.dumps(calls[-1]["input"]))

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
        compactions = []
        fail_compaction = False
        @contextmanager
        def provider(body, headers):
            calls.append(json.loads(body))
            n = len(calls)
            stream = reply(n, {"type": "message", "role": "assistant", "id": f"msg_{n}", "status": "completed",
                               "content": [{"type": "output_text", "text": "Synthetic chess note."}]})
            if fail_compaction and not calls[-1]["tools"]:
                stream.status = 403
            yield stream
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
                                                on_compaction=lambda *event: compactions.append(event),
                                                dispatch=lambda *_: {}, thread_id=tid, base_url=gate.base_url,
                                                env={"LANTERN_GATEWAY_TOKEN": gate.token}, timeout=28))
            with patch.object(_Peer, "finish_turn", compact):
                tid = run([old]).thread_id
            started, completed = compactions
            self.assertEqual((started[0], started[2], completed[2]), (tid, "started", "completed"))
            self.assertEqual(started[:2], completed[:2])
            self.assertIsInstance(started[3], int)
            self.assertGreaterEqual(completed[3], started[3])
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
            self.assertEqual(len(compactions), 2)  # Resuming does not replay previous completions.
            fail_compaction = True
            with patch.object(_Peer, "finish_turn", compact), self.assertRaises(CodexError):
                run([revised], tid)
            self.assertEqual([event[2] for event in compactions], ["started", "completed", "started"])
