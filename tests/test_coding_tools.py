from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from lantern.coding_tools import CODING_TOOL, COMMAND_LIMIT, run_code
from lantern.supervision import Supervisor, supervised_tools
from lantern.thread_upgrade import install_room_tools
from lantern.transport import MODEL, ProtocolError, _prepare


class CodingClientTests(unittest.TestCase):
    def setUp(self):
        self.socket = self.enterContext(patch("lantern.coding_tools.socket.socket", autospec=True))
        self.enterContext(patch("lantern.coding_tools.socket.AF_UNIX", 1, create=True))
        self.peer = self.socket.return_value.__enter__.return_value

    def test_delimited_response_and_exact_multiline_script(self):
        command = "python3 - <<'PY'\nprint('♟')\nPY"
        response = {"exit_code": 0, "output": "♟\n", "truncated": False, "timed_out": False}
        frame = json.dumps(response, ensure_ascii=False).encode() + b"\n"
        self.peer.recv.side_effect = [frame[:3], frame[3:]]
        self.assertEqual(run_code("broker.sock", {"command": command}), response)
        self.peer.connect.assert_called_once_with("broker.sock")
        self.peer.sendall.assert_called_once()
        sent = self.peer.sendall.call_args.args[0]
        self.assertEqual(sent.count(b"\n"), 1)
        self.assertTrue(sent.endswith(b"\n"))
        self.assertEqual(json.loads(sent), {"command": command})

    def test_input_validation_precedes_connection(self):
        for args in ({"command": ""}, {"command": "A\0B"}, {"command": "α" * COMMAND_LIMIT},
                     {"command": "pwd", "path": "/tmp/elsewhere"}):
            with self.subTest(args=str(args)[:30]), self.assertRaises(ValueError):
                run_code("broker.sock", args)
        self.socket.assert_not_called()

    def test_connected_disconnect_is_cached_by_supervision_without_reexecution(self):
        self.peer.recv.return_value = b""
        with TemporaryDirectory() as folder:
            luna, opus = Mock(), Mock()
            luna.screen.return_value = {"escalate": False, "summary": "Edit a test file.", "evidence": ""}
            supervisor = Supervisor(Path(folder), luna, opus, Mock(), tool_names=["coding_run"])
            supervisor.bind("offline-thread")
            supervisor.observe({"input": "Write the fixture.", "tools": [CODING_TOOL]})
            args = {"command": "printf 'sample\\n' >> sample.txt", "rationale": "Add the requested fixture."}
            execute = Mock(side_effect=lambda name, reviewed: run_code("broker.sock", reviewed))
            result = supervisor.dispatch("id", "coding_run", args, execute)
            self.assertIn("may have run", result["error"])
            self.assertEqual(supervisor.dispatch("id", "coding_run", args, execute), result)
            execute.assert_called_once_with("coding_run", {"command": args["command"]})
            signals = [c.args[0] for c in luna.screen.call_args_list if c.args[0]["kind"] == "tool"]
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["arguments"], {"command": args["command"]})
            opus.assert_not_called()
            self.peer.sendall.assert_called_once()

    def test_unavailable_or_malformed_runner(self):
        self.peer.connect.side_effect = ConnectionRefusedError
        self.assertIn("not sent", run_code("broker.sock", {"command": "pwd"})["error"])
        self.peer.sendall.assert_not_called()
        self.peer.connect.side_effect = None
        self.peer.recv.return_value = b'{"exit_code":0,"output":null}\n'
        self.assertIn("may have run", run_code("broker.sock", {"command": "pwd"})["error"])
        self.peer.recv.side_effect = TimeoutError
        self.assertIn("may have run", run_code("broker.sock", {"command": "pwd"})["error"])

    def test_disabled_runner_preserves_history_without_advertising_tool(self):
        history = [{"type": "function_call", "name": "coding_run", "call_id": "prior"},
                   {"type": "function_call_output", "call_id": "prior", "output": "ok"},
                   {"type": "function_call", "name": "harness_logs", "call_id": "logs"},
                   {"type": "function_call_output", "call_id": "logs", "output": "ok"}]
        payload = {"model": MODEL, "stream": True, "input": history,
                   "tools": [{"type": "function", "name": "coding_run"}]}
        prepared = _prepare(payload, "fastest", [], resuming=True)
        self.assertEqual(prepared["input"], history)
        self.assertEqual(prepared["tools"], [])
        with self.assertRaises(ProtocolError):
            _prepare({**payload, "tool_choice": payload["tools"][0]}, "fastest", [], resuming=True)


class CodingRegistryTests(unittest.TestCase):
    def test_second_upgrade_uses_current_rollout_and_recovers_without_rewinding(self):
        with TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            home = state / "codex"
            (home / "sessions").mkdir(parents=True)
            tid = str(uuid4())
            rollout = home / "sessions" / f"rollout-{tid}.jsonl"
            initial = [{"type": "session_meta", "payload": {"id": tid, "dynamic_tools": []}},
                       {"type": "event_msg", "payload": {"text": "first β turn"}}]
            def line(item):
                return (json.dumps(item, ensure_ascii=False) + "\n").encode()
            rollout.write_bytes(b"".join(line(item) for item in initial))
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads(id, rollout_path)")
                db.execute("INSERT INTO threads VALUES(?,?)", (tid, str(rollout)))
            with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db, db:
                db.execute("CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,rollout_end_ordinal,"
                           "rollout_byte_offset,rollout_end_byte_offset)")
                db.execute("CREATE TABLE thread_history_projection_state(thread_id,next_rollout_ordinal,"
                           "next_rollout_byte_offset)")
                db.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (tid, 2, rollout.stat().st_size))
            old = {"name": "workspace_write", "description": "Save", "inputSchema": {"type": "object", "properties": {}}}
            logs = {"name": "harness_logs", "description": "Inspect", "inputSchema": {"type": "object", "properties": {}}}
            install_room_tools(state, tid, supervised_tools([old]))
            base_backup = next((state / "operator-backups/independent-work-v1").glob("*.jsonl"))
            self.assertEqual(base_backup.read_bytes(), b"".join(map(line, initial)))
            second_start = rollout.stat().st_size
            with rollout.open("ab") as stream:
                stream.write(line({"type": "event_msg", "payload": {"text": "later turn & compaction ♟"}}))
            before = rollout.read_bytes()
            with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db, db:
                db.execute("INSERT INTO thread_turns VALUES(?,?,2,2,?,?)", (tid, "turn2", second_start, len(before)))
                db.execute("UPDATE thread_history_projection_state SET next_rollout_ordinal=3,next_rollout_byte_offset=?",
                           (len(before),))
            # Simulate a service failure after the file rename, before the index commit.
            original_replace = os.replace
            def replace_then_interrupt(src, dst):
                original_replace(src, dst)
                if dst == rollout:
                    raise RuntimeError("injected interruption")
            all_tools = supervised_tools([old, CODING_TOOL])
            with patch("lantern.thread_upgrade.os.replace", side_effect=replace_then_interrupt), self.assertRaises(RuntimeError):
                install_room_tools(state, tid, all_tools)
            coding_backup = next((state / "operator-backups/coding-workspace-v1").glob("*.jsonl"))
            self.assertEqual(coding_backup.read_bytes(), before)
            self.assertFalse(coding_backup.with_suffix(".done").exists())
            # Roll back the runner switch and enable diagnostics in the same
            # restart. The pending offset repair must precede the new migration.
            install_room_tools(state, tid, supervised_tools([old, logs]))
            self.assertTrue(coding_backup.with_suffix(".done").exists())
            after = rollout.read_bytes()
            self.assertEqual(after.splitlines(keepends=True)[1:], before.splitlines(keepends=True)[1:])
            names = [tool["name"] for tool in json.loads(after.splitlines()[0])["payload"]["dynamic_tools"]]
            self.assertEqual(names, ["workspace_write", "consult_opus", "coding_run", "harness_logs"])
            logs_backup = next((state / "operator-backups/harness-logs-v1").glob("*.jsonl"))
            after_coding = logs_backup.read_bytes()
            self.assertEqual(after_coding.splitlines(keepends=True)[1:], before.splitlines(keepends=True)[1:])
            self.assertNotEqual(after, after_coding)
            with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db:
                self.assertEqual(db.execute("SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns").fetchone(),
                                 (second_start + len(after) - len(before), len(after)))
                self.assertEqual(db.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state").fetchone(),
                                 (len(after),))
            with rollout.open("ab") as stream:
                stream.write(line({"type": "event_msg", "payload": {"text": "later still"}}))
            final = rollout.read_bytes()
            self.assertIsNone(install_room_tools(state, tid, supervised_tools([old, CODING_TOOL, logs])))
            self.assertEqual(rollout.read_bytes(), final)
            self.assertEqual(coding_backup.read_bytes(), before)
