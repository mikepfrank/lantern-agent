"""Opt in with LANTERN_TEST_CODEX=1 and a native codex binary on PATH.

Only the remote Responses endpoint is replaced. Native Codex, the on-disk thread,
the HTTP gateway, budget ledger, persona loading and the chess engine are real.
"""

import argparse
import asyncio
from contextlib import contextmanager, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.__main__ import MATE_FEN, main
from lantern.transport import MODEL


class FixtureStream(BytesIO):
    status = 200

    def getheader(self, name, default=None):
        return "text/event-stream" if name == "Content-Type" else default


def reply(serial, item):
    response = {"id": f"resp_fixture_{serial}", "model": MODEL}
    if item["type"] == "function_call":
        initial = {**item, "status": "in_progress", "arguments": ""}
        delta = {"type": "response.function_call_arguments.delta", "delta": item["arguments"]}
    else:
        initial = {**item, "status": "in_progress", "content": []}
        delta = {"type": "response.output_text.delta", "content_index": 0, "delta": item["content"][0]["text"]}
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": initial},
        {**delta, "output_index": 0, "item_id": item["id"]},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
            "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "cost": 0.00001}}},
    ]
    return FixtureStream(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events))


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "opt in with LANTERN_TEST_CODEX=1")
class NativeCodexSmoke(unittest.TestCase):
    def test_native_thread_tool_roundtrip_and_resume(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native codex executable")
        persona_file = Path(__file__).resolve().parents[1] / "lantern" / "personas" / "lantern-v1.md"
        persona = persona_file.read_text(encoding="utf-8")
        routes = []
        errors = []
        child_pids = []
        start_process = asyncio.create_subprocess_exec

        async def record_process(*args, **kwargs):
            process = await start_process(*args, **kwargs)
            child_pids.append(process.pid)
            return process

        @contextmanager
        def fake_provider(body, headers):
            try:
                request = json.loads(body)
                self.assertEqual(request["model"], MODEL)
                self.assertEqual([t["name"] for t in request["tools"]], ["chess_analyze"])
                self.assertEqual(headers["Authorization"], "Bearer offline-test-value")
                routes.append(request["provider"]["sort"])
                serial = len(routes)
                history = request["input"]
                first_user = next(index for index, item in enumerate(history) if item.get("role") == "user")
                developer_texts = [
                    part["text"] for message in history[:first_user] if message.get("role") == "developer"
                    for part in message["content"] if part["type"] == "input_text"
                ]
                self.assertTrue(any(persona in text for text in developer_texts),
                                f"Request {serial} is missing the full persona before its first user message")
                users = [json.dumps(i["content"]) for i in history if i.get("role") == "user"]
                ticket = re.search(r"Beacon-[0-9a-f]+", " ".join(users))
                self.assertIsNotNone(ticket, "operator ticket missing from the actual native thread")
                if serial == 1:
                    self.assertIn(MATE_FEN, users[-1])
                    item = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
                            "status": "completed", "name": "chess_analyze",
                            "arguments": json.dumps({"fen": MATE_FEN})}
                else:
                    outputs = [i["output"] for i in history if i.get("type") == "function_call_output"
                               and i.get("call_id") == "call_fixture"]
                    self.assertEqual(len(outputs), 1)
                    self.assertIn("Qg7#", json.dumps(outputs[0]))
                    if serial == 2:
                        answer = "Qg7# is checkmate."
                    else:
                        self.assertEqual(serial, 3, "unexpected extra API request")
                        self.assertIn("preceding exchange", users[-1])
                        self.assertNotIn(ticket.group(), users[-1])
                        answer = f"{ticket.group()} Qg7#"
                    item = {"type": "message", "role": "assistant", "id": f"msg_fixture_{serial}",
                            "status": "completed", "content": [{"type": "output_text", "annotations": [],
                                                                  "text": answer}]}
            except (AssertionError, IndexError, KeyError, TypeError) as error:
                errors.append(str(error))
                raise
            yield reply(serial, item)

        with TemporaryDirectory(prefix="lantern-smoke-") as tmp:
            state = Path(tmp)
            args = argparse.Namespace(state=state, persona=None, codex=binary, command="smoke")
            with patch.dict(os.environ, OPENROUTER_API_KEY="offline-test-value"), \
                 patch("lantern.transport._open_upstream", fake_provider), \
                 patch("asyncio.create_subprocess_exec", record_process), redirect_stdout(StringIO()) as output:
                try:
                    asyncio.run(main(args))
                finally:
                    self.assertEqual(errors, [])
        self.assertEqual(routes, ["throughput", "throughput", "price"])
        self.assertEqual(len(set(child_pids)), 2)
        self.assertIn('"smoke": "passed"', output.getvalue())
