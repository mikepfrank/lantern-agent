"""Exercise native tool-result images and their provenance without contacting a vendor."""

import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.codex_session import run_turn
from lantern.image_ref import image_reference
from lantern.luna import _screening_content
from lantern.supervision import recent_messages
from lantern.transport import MODEL, OpenRouterGateway, ProtocolError, _prepare_input
from test_attachments import png
from test_codex_smoke import reply


TOOL = {"name": "view_pdf_page", "description": "Render a document page",
        "inputSchema": {"type": "object", "properties": {}}}
DATA = png()
DIGEST = hashlib.sha256(DATA).hexdigest()
URL = "data:image/png;base64," + base64.b64encode(DATA).decode("ascii")
REF = image_reference(42, DIGEST, page=True)


def call(call_id):
    return {"type": "function_call", "name": "view_pdf_page", "call_id": call_id, "arguments": "{}"}


def output(call_id, uri):
    return {"type": "function_call_output", "call_id": call_id, "output": [
        {"type": "input_text", "text": "The document page follows."}, {"type": "input_image", "image_url": uri}]}


class PageTransport(unittest.TestCase):
    @staticmethod
    def prepare(items):
        return _prepare_input(items, {"view_pdf_page", "coding_run"}, lambda n: DATA,
                              lambda n, cid: DATA if (n, cid) == (42, "p1") else None)

    def test_reject_wrong_origin_call_or_digest(self):
        operator = image_reference(42, DIGEST)
        for items in ([{"role": "user", "content": [{"type": "input_image", "image_url": REF}]}],
                      [call("p1"), output("p1", operator)],
                      [call("p2"), output("p2", REF)],
                      [call("p1"), output("p2", REF)],
                      [dict(call("p1"), name="coding_run"), output("p1", REF)],
                      [output("p1", REF)],
                      [call("p1"), output("p1", image_reference(42, "a" * 64, page=True))]):
            with self.subTest(items=items), self.assertRaises(ProtocolError):
                self.prepare(items)

    def test_tool_and_operator_images_share_the_same_window(self):
        user = {"role": "user", "content": [{"type": "input_image", "image_url": image_reference(1, DIGEST)}]}
        inputs = self.prepare([user] * 4 + [call("p1"), output("p1", REF)])
        self.assertIn("archived", inputs[0]["content"][0]["text"])
        self.assertEqual(inputs[-1]["output"][-1]["image_url"], URL)
        self.assertEqual(sum("image_url" in part for item in inputs
                             for part in item.get("output", item.get("content", []))), 4)
        recent = recent_messages({"input": inputs})
        self.assertIn("[Rendered PDF page from tool]", recent)
        self.assertNotIn("base64", recent)
        screened = _screening_content({"kind": "context", "context": {"input": inputs}})
        self.assertEqual(len(screened[1:]), 4)
        self.assertEqual(screened[-1]["image_url"], URL)
        self.assertNotIn("base64", screened[0]["text"])
        labels = json.loads(screened[0]["text"])["context"]["input"]
        self.assertEqual(labels[-1]["output"][-1], {"type": "rendered_pdf_page", "attachment": 4})
        self.assertEqual(labels[1]["content"][0], {"type": "operator_image", "attachment": 1})

    @unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
    def test_native_result_and_cross_process_replay(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary)
        requests, dispatches = [], []

        @contextmanager
        def provider(body, headers):
            requests.append(json.loads(body))
            n = len(requests)
            if n == 1:
                item = {**call("p1"), "id": "fc_1", "status": "completed"}
            else:
                item = {"type": "message", "role": "assistant", "id": f"msg_{n}", "status": "completed",
                        "content": [{"type": "output_text", "text": "Received."}]}
            yield reply(n, item)

        def dispatch(call_id, name, args):
            dispatches.append((call_id, name))
            return {"page": 7, "page_image": {"id": 42, "sha256": DIGEST}}

        with TemporaryDirectory() as tmp, patch("lantern.transport._open_upstream", provider):
            home, work = Path(tmp) / "codex", Path(tmp) / "work"
            home.mkdir()
            work.mkdir()
            tid = None
            for prompt in ("Read the rendered page.", "Recall the image."):
                with OpenRouterGateway("fixture", route="fastest", tools=[TOOL], reserve=lambda _: 1,
                                       settle=lambda *_: None, observe=lambda *_: None, unregistered=lambda *_: None,
                                       page_image_loader=lambda n, cid: DATA if (n, cid) == (42, "p1") else None) as gate:
                    result = asyncio.run(run_turn(codex_bin=binary, codex_home=home, workspace=work, model=MODEL,
                                                  provider="lantern", persona="A fixture.", prompt=prompt, tools=[TOOL],
                                                  dispatch=dispatch, thread_id=tid, base_url=gate.base_url,
                                                  env={"LANTERN_GATEWAY_TOKEN": gate.token}, timeout=25))
                    self.assertEqual(result.text, "Received.")
                    if tid:
                        self.assertEqual(tid, result.thread_id)
                    tid = result.thread_id
            rollout, = (home / "sessions").rglob("rollout-*.jsonl")
            saved = rollout.read_text(encoding="utf-8")
            self.assertIn(REF, saved)
            self.assertNotIn(URL, saved)
        self.assertEqual(dispatches, [("p1", "view_pdf_page")])
        self.assertEqual(len(requests), 3)
        for request in requests[1:]:
            page, = [row for row in request["input"] if row.get("type") == "function_call_output"]
            self.assertEqual(page["call_id"], "p1")
            self.assertEqual(page["output"][1]["image_url"], URL)
            self.assertNotIn("base64", page["output"][0]["text"])
