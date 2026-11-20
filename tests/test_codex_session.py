import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lantern.codex_session import CodexError, _Peer, _child_environment


class ChildEnvironmentTests(unittest.TestCase):
    def test_service_credentials_stay_in_the_parent(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "upstream-only", "SSH_AUTH_SOCK": "/operator/socket",
                "ANTHROPIC_API_KEY": "supervisor-only", "CODEX_HOME": "/operator/home"}):
            home = Path(tmp)
            env = _child_environment(home, {"LANTERN_GATEWAY_TOKEN": "turn-only"})
            self.assertEqual(env["CODEX_HOME"], str(home))
            self.assertEqual(env["LANTERN_GATEWAY_TOKEN"], "turn-only")
            self.assertFalse({"OPENROUTER_API_KEY", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY"} & env.keys())
            with self.assertRaises(CodexError):
                _child_environment(home, {"OPENROUTER_API_KEY": "wrong"})


class ToolRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dispatches = []
        self.sent = []

        def chess(call_id, name, args):
            self.dispatches.append((call_id, name, args))
            return {"best": "Qg7#"}

        async def save(message):
            self.sent.append(message)

        self.peer = _Peer(None, [{"name": "chess_analyze"}], chess)
        self.peer.send = save
        self.peer.thread_id = "thread"
        self.peer.set_turn("turn")
        self.params = {"tool": "chess_analyze", "threadId": "thread", "turnId": "turn",
                       "callId": "call", "arguments": {"fen": "position"}}

    async def test_chess_result_returns_on_the_call(self):
        await self.peer.handle({"id": 17, "method": "item/tool/call", "params": self.params})
        response, = self.sent
        self.assertEqual(self.dispatches, [("call", "chess_analyze", {"fen": "position"})])
        self.assertEqual(response["id"], 17)
        self.assertTrue(response["result"]["success"])
        self.assertEqual(json.loads(response["result"]["contentItems"][0]["text"]), {"best": "Qg7#"})

    async def test_completed_segments_have_clean_boundaries_and_keep_code(self):
        for phase, text in ((None, "\r\n\n  First. \t\n"), (None, "\n  \n"),
                            ("commentary", "Private progress."), (None, "\r\n    line()\n    next()\n"),
                            ("final_answer", "  \n\n```\na\n\n\nb\n```\n\n")):
            item = {"type": "agentMessage", "phase": phase, "text": text}
            await self.peer.handle({"method": "item/completed", "params": {"item": item}})
            self.assertEqual(item["text"], text)
        self.assertEqual(self.peer.messages, ["First.", "    line()\n    next()", "```\na\n\n\nb\n```"])

    async def test_other_threads_turns_and_unregistered_tools_cannot_dispatch(self):
        for invalid in ({"threadId": "another"}, {"turnId": "another"}, {"tool": "shell"},
                        {"namespace": "another"}, {"arguments": []}, {"callId": None}):
            with self.subTest(invalid=invalid), self.assertRaises(CodexError):
                await self.peer.handle({"id": 18, "method": "item/tool/call",
                                        "params": {**self.params, **invalid}})
        self.assertFalse(self.dispatches)
