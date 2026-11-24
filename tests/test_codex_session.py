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

    async def test_each_finished_public_item_notifies_once_even_if_the_turn_later_fails(self):
        delivered = []
        def observe(item_id, text):
            delivered.append((item_id, text))
        self.peer.on_message = observe
        params = {"threadId": "thread", "turnId": "turn"}

        async def emit(method, item_id, kind="agentMessage", text=" text ", **values):
            item = {"id": item_id, "type": kind, "text": text, **values}
            await self.peer.handle({"method": method, "params": {**params, "item": item}})

        await emit("item/started", "first")
        await emit("item/agentMessage/delta", "first")
        await emit("item/completed", "internal", kind="reasoning")
        await emit("item/completed", "private", phase="commentary")
        await emit("item/completed", "tool", kind="dynamicToolCall")
        await emit("item/completed", "empty", text=" \t\n")
        self.assertEqual(delivered, [])
        await emit("item/completed", "first")
        self.assertEqual(delivered, [("first", "text")])
        self.assertFalse(self.peer.finished)
        await emit("item/completed", "first")
        await emit("item/completed", "second", phase="final_answer")
        self.assertEqual(delivered, [("first", "text"), ("second", "text")])
        self.assertEqual(self.peer.messages, ["text", "text"])
        with self.assertRaises(CodexError):
            await self.peer.handle({"method": "turn/completed", "params": {
                "threadId": "thread", "turn": {"id": "turn", "status": "failed"}}})
        self.assertEqual(len(delivered), 2)

    async def test_publication_requires_active_thread_turn_and_stable_item_identity(self):
        delivered = []
        self.peer.on_message = lambda item_id, text: delivered.append((item_id, text))
        item = {"id": "msg", "type": "agentMessage", "text": "First."}
        params = {"threadId": "thread", "turnId": "turn", "item": item}
        async def complete(**changed):
            await self.peer.handle({"method": "item/completed", "params": {**params, **changed}})

        self.peer.thread_id = None
        await complete()
        self.assertEqual(delivered, [])
        self.peer.thread_id = "thread"
        for invalid in ({"threadId": None}, {"threadId": "wrong"}, {"turnId": None},
                        {"turnId": "another-turn"}, {"item": {**item, "id": ""}}):
            with self.subTest(invalid=invalid), self.assertRaises(CodexError):
                await complete(**invalid)
        await complete()
        with self.assertRaisesRegex(CodexError, "reused"):
            await complete(item={**item, "text": "Changed."})
        self.peer.finished = True
        await complete(item={**item, "id": "late"})
        self.assertEqual(delivered, [("msg", "First.")])

    async def test_other_threads_turns_and_unregistered_tools_cannot_dispatch(self):
        for invalid in ({"threadId": "another"}, {"turnId": "another"}, {"tool": "shell"},
                        {"namespace": "another"}, {"arguments": []}, {"callId": None}):
            with self.subTest(invalid=invalid), self.assertRaises(CodexError):
                await self.peer.handle({"id": 18, "method": "item/tool/call",
                                        "params": {**self.params, **invalid}})
        self.assertFalse(self.dispatches)
