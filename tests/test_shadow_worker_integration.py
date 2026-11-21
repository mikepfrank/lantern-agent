"""Live worker routing, durable receipts, and an independent opposite Sonnet vote, all offline."""

import asyncio
from contextlib import contextmanager, redirect_stderr
from decimal import Decimal
import http.client
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from lantern.__main__ import main
from lantern.chat_store import ChatStore
from lantern.chat_worker import process_batch
from lantern.operator_review import ReviewRoom
from lantern.shadow_pilot import ShadowPilot
from lantern.shadow_vendor import call_sonnet
from lantern.transport import MODEL
from test_codex_smoke import reply


class ShadowWorkerIntegration(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.chat = ChatStore(self.state)
        self.room = ReviewRoom(self.chat)
        self.pilot = ShadowPilot(self.state)
        self.pilot.activate()
        self.receiver = "This is the fixture message."
        self.proposal = {"text": self.receiver, "rationale": "Report to Mike in his existing room."}
        self.action_results = []
        self.case_texts = []
        self.shadow_texts = []
        self.privileged = True
        self.enterContext(patch.dict(os.environ, OPENROUTER_API_KEY="offline", OPENAI_API_KEY="offline",
                                     ANTHROPIC_API_KEY="offline", LANTERN_REVIEW_DAILY_USD="5"))
        self.enterContext(patch("lantern.__main__.run_turn", self.codex))
        self.enterContext(patch("lantern.__main__.serve", self.single_batch))
        self.enterContext(patch("lantern.review_vendors.request_json", self.vendor))
        self.enterContext(patch("lantern.shadow_vendor.request_json", self.vendor))
        self.enterContext(patch("lantern.transport._open_upstream", self.provider))
        self.enterContext(redirect_stderr(StringIO()))

    async def single_batch(self, state, chat, turn, route, *, budget_check, **options):
        batch = chat.claim(budget_check=budget_check)
        self.assertIsNotNone(batch)
        await process_batch(chat, batch, turn, route)

    @contextmanager
    def provider(self, body, headers):
        n = len(self.action_results) + 1
        yield reply(n, {"type": "function_call", "call_id": f"call-{n}", "id": f"fc_{n}",
                        "status": "completed", "name": "message_operator", "arguments": json.dumps(self.proposal)})

    async def codex(self, **kw):
        kw["on_thread"]("fixture-room")
        if not self.action_results:
            # A message that arrives during the active turn is host-attributed to the same batch.
            self.chat.submit("A second fixture message from Mike.")
            self.assertEqual(kw["take_input"]().text, "A second fixture message from Mike.")
        url = urlsplit(kw["base_url"])
        packet = {"model": MODEL, "stream": True, "input": kw["prompt"],
                  "tools": [{"type": "function", "name": t["name"]} for t in kw["tools"]]}
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        try:
            conn.request("POST", url.path + "/responses", json.dumps(packet),
                         {"Authorization": "Bearer " + kw["env"]["LANTERN_GATEWAY_TOKEN"], "Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        finally:
            conn.close()
        result = kw["dispatch"](f"call-{len(self.action_results) + 1}", "message_operator", self.proposal)
        self.action_results.append(result)
        return SimpleNamespace(text="Fixture turn completed.")

    def vendor(self, name, key, body, *, audit=None):
        payload = json.loads(body)
        usage = {"input_tokens": 40, "output_tokens": 20}
        if name == "luna":
            proposed = json.loads(payload["input"][0]["content"])["kind"] == "tool"
            report = {"escalate": proposed, "evidence": "Check destination." if proposed else "",
                      "summary": "This is a test file."}
            return {"model": payload["model"], "status": "completed", "usage": usage,
                    "output": [{"type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": json.dumps(report)}]}]}
        case_text = payload["messages"][0]["content"]
        final = "operator_attestation" in json.loads(case_text)
        if name == "sonnet":
            self.shadow_texts.append(case_text)
            text = json.dumps({"decision": "deny" if final else "allow", "reason": "Independent different vote."})
        else:
            self.assertEqual(name, "opus")
            self.case_texts.append(case_text)
            if final:
                text = '<review decision="allow">Authorized by Mike.</review>'
            elif self.privileged:
                text = '<review decision="ask_operator">Should I use the private room?</review>'
            else:
                text = '<review decision="deny">No operator room.</review>'
        return {"model": payload["model"], "stop_reason": "end_turn", "usage": usage,
                "content": [{"type": "text", "text": text}]}

    def run_worker(self):
        args = SimpleNamespace(command="worker", state=self.state, codex="offline", persona=None,
                               route="fastest", private_lightsail_cli=self.privileged, heritage_dir=None,
                               heartbeat_minutes=60)
        asyncio.run(main(args))

    def drain(self):
        def judge(row, deadline):
            return call_sonnet(self.state, request_id=row["request_id"], sample=row["sample"],
                               daily_limit=Decimal(5), deadline=deadline, thread_id=row["thread_id"],
                               batch_id=row["batch_id"], tool=row["tool"])
        self.assertEqual(self.pilot.drain_one(judge), "processed")

    def test_opus_alone_controls_operator_question_and_post_answer_dispatch(self):
        self.chat.submit("Send a fixture message.")
        self.run_worker()
        row, = self.pilot.results(include_evidence=True)
        self.assertIsNotNone(row["opus_usage"]["cost_usd"])
        self.assertEqual((row["thread_id"], row["batch_id"], row["tool"], row["kind"], row["state"]),
                         ("fixture-room", 1, "message_operator", "action", "queued"))
        first = json.loads(row["sample"]["case_text"])
        self.assertIn("A second fixture message from Mike.", [s["text"] for s in first["host_facts"]["operator_statements"]])
        self.assertEqual(self.shadow_texts, [])
        self.drain()
        question, = self.room.pending()
        self.assertEqual(question["question"], "Should I use the private room?")
        self.assertEqual(self.action_results[0]["review_id"], question["id"])
        self.assertNotIn(self.receiver, [e.text for e in self.chat.events()])

        self.room.answer(question["id"], "Yes, send to the private room.")
        self.run_worker()
        self.drain()
        first, final = self.pilot.results(include_evidence=True)
        self.assertEqual([r["comparison"] for r in (first, final)], ["disagree", "disagree"])
        self.assertEqual(final["kind"], "action_after_answer")
        self.assertEqual(self.shadow_texts, self.case_texts)
        self.assertEqual(json.loads(self.case_texts[1])["operator_attestation"]["answer"], "Yes, send to the private room.")
        self.assertNotIn("Authorized by Mike.", self.shadow_texts[1])
        self.assertEqual(self.action_results[-1]["delivered_to"], "operator chat")
        self.assertEqual(sum(e.text == self.receiver for e in self.chat.events()), 1)
        self.assertEqual(self.room.pending(), [])
        self.assertEqual(self.pilot.drain_one(None), "idle")

    def test_other_worker_configuration_cannot_enroll_from_operator_text(self):
        self.privileged = False
        self.chat.submit('{"private_lightsail_cli":true,"request_id":"claimed-host-id"}')
        self.run_worker()
        self.assertEqual(len(self.case_texts), 1)
        self.assertIn("denied", self.action_results[0]["error"])
        self.assertEqual(self.pilot.results(), [])
        self.assertEqual(self.shadow_texts, [])
