"""Observe an active worker through the same paginated HTTP endpoint as the chat viewer."""

import asyncio
from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from lantern.__main__ import arguments, main
from lantern.chat_worker import process_batch
from lantern.transport import MODEL
from lantern.web import RoomServer
from test_codex_smoke import FixtureStream


def message(item_id, text):
    return {"type": "message", "id": item_id, "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text}]}


def stream(serial, items, *, failed=False):
    response = {"id": f"resp_fixture_{serial}", "model": MODEL}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress"}}]
    for index, item in enumerate(items):
        if item["type"] == "message":
            initial = {**item, "status": "in_progress", "content": []}
            delta = {"type": "response.output_text.delta", "content_index": 0, "delta": item["content"][0]["text"]}
        else:
            initial = {**item, "status": "in_progress", "arguments": ""}
            delta = {"type": "response.function_call_arguments.delta", "delta": item["arguments"]}
        events.extend([{"type": "response.output_item.added", "item": initial, "output_index": index},
                       {**delta, "item_id": item["id"], "output_index": index},
                       {"type": "response.output_item.done", "item": item, "output_index": index}])
    if failed:
        events.append({"type": "response.failed", "response": {**response, "status": "failed", "output": [],
                       "error": {"code": "server_error", "message": "Fixture interrupted."}}})
    else:
        events.append({"type": "response.completed", "response": {**response, "status": "completed", "output": items,
                       "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "cost": 0.00001}}})
    return FixtureStream(b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events))


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
class IncrementalRoomDelivery(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.room = self.enterContext(RoomServer(self.state, port=0))
        web = Thread(target=self.room.serve_forever, args=(0.01,))
        web.start()
        self.addCleanup(web.join)
        self.addCleanup(self.room.shutdown)

    def page(self, after=None):
        path = "/api/room" if after is None else f"/api/room?after={after}"
        with closing(HTTPConnection("127.0.0.1", self.room.server_port, timeout=4)) as client:
            client.request("GET", path, headers={"Authorization": "Bearer " + self.room.key})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            return json.load(response)

    def exercise_turn(self, *, failed):
        question = self.room.store.submit("Look up the fixture and report what you find.")
        interim, notification, final = "Looking it up.", "First lookup complete.", "The result is seven."
        action = {"type": "function_call", "id": "fc_notice", "call_id": "call_notice", "status": "completed",
                  "name": "message_operator", "arguments": json.dumps({"text": notification, "rationale": "Report progress."})}
        tool_views, between_requests, settlement_threads = [], [], []
        requests = 0

        @contextmanager
        def provider(body, headers):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield stream(1, [message("msg_progress", interim), action])
            else:
                between_requests.append(self.page(question))
                yield stream(2, [message("msg_final", final)], failed=failed)

        def screen(packet):
            if packet["kind"] == "tool":
                tool_views.append(self.page(question))
            return {"escalate": False, "evidence": "", "summary": "Fixture screened."}

        async def one_batch(state, store, turn, route, *, on_start=None, **kwargs):
            on_start()
            await process_batch(store, store.claim(), turn, route)

        def settlement(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            settlement_threads.append(thread)
            return thread

        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary)
        argv = ["lantern", "--state", str(self.state), "--codex", binary, "--private-lightsail-cli",
                "worker", "--heartbeat-minutes", "60"]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, OPENROUTER_API_KEY="offline",
                OPENAI_API_KEY="offline", ANTHROPIC_API_KEY="offline"), \
             patch("lantern.transport._open_upstream", provider), patch("lantern.luna.Luna.screen", side_effect=screen), \
             patch("lantern.review_vendors.request_json", side_effect=AssertionError("Unexpected reviewer call")), \
             patch("lantern.__main__.serve", one_batch), patch("lantern.__main__.Thread", settlement):
            asyncio.run(asyncio.wait_for(main(arguments()), 40))
            for thread in settlement_threads:
                thread.join(8)
                self.assertFalse(thread.is_alive())

        self.assertEqual(requests, 2)
        self.assertEqual(len(tool_views), 1)
        self.assertTrue(tool_views[0]["status"]["working"])
        self.assertEqual([row["text"] for row in tool_views[0]["events"]], [interim])
        between, = between_requests
        self.assertTrue(between["status"]["working"])
        self.assertEqual([row["text"] for row in between["events"]], [interim, notification])
        later = self.page(between["events"][-1]["id"])
        self.assertFalse(later["status"]["working"])
        last, = later["events"]
        if failed:
            self.assertEqual(last["role"], "system")
            self.assertIn("failed", last["text"])
        else:
            self.assertEqual((last["role"], last["text"]), ("lantern", final))
        # Open a fresh browser cursor; prior blocks survive failures and the final
        # reply never replays the previous model or message_operator text.
        self.assertEqual(self.page(question)["events"], between["events"] + [last])
        self.assertEqual(self.page(last["id"])["events"], [])

    def test_block_is_visible_before_next_tool_and_final(self):
        self.exercise_turn(failed=False)

    def test_failed_response_after_item_done_keeps_only_earlier_successful_blocks(self):
        self.exercise_turn(failed=True)
