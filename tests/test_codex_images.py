"""Exercise native image input and saved-thread replay without contacting any vendor."""

import asyncio
import base64
from contextlib import contextmanager, redirect_stderr
from io import StringIO
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
from lantern.attachments import parse_image
from lantern.chat_store import ChatStore
from lantern.chat_worker import process_batch
from lantern.image_ref import image_reference
from test_attachments import png, upload
from test_codex_smoke import reply


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
class NativeImageInput(unittest.TestCase):
    def test_room_image_and_caption_survive_a_native_process_restart(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native Codex executable")
        source = parse_image(upload(png()))
        encoded = base64.b64encode(source.data).decode("ascii")
        caption = "Describe this private diagram."
        followup = "Does my earlier diagram remain visible?"
        requests, screens, settlements = [], [], []

        @contextmanager
        def provider(body, headers):
            requests.append(json.loads(body))
            serial = len(requests)
            yield reply(serial, {"type": "message", "role": "assistant", "id": f"msg_{serial}",
                                 "status": "completed", "content": [{"type": "output_text", "text": "Received."}]})

        def reviewer(vendor, key, body):
            self.assertEqual(vendor, "luna")
            payload = json.loads(body)
            screens.append(payload["input"][0]["content"])
            report = json.dumps({"escalate": False, "summary": "Operator image fixture.", "evidence": ""})
            return {"model": payload["model"], "status": "completed", "usage": {"input_tokens": 80, "output_tokens": 15},
                    "output": [{"type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": report}]}]}

        # Run one queued batch per invocation so the second worker reopens the
        # first native thread and attachment database just as after a disconnect.
        async def one_batch(state, store, turn, route, *, on_start=None, **kwargs):
            if on_start:
                on_start()
            await process_batch(store, store.claim(), turn, route)

        def settlement(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            settlements.append(thread)
            return thread

        def boot(state):
            argv = ["lantern", "--state", str(state), "--codex", binary, "--private-lightsail-cli", "worker"]
            with patch.object(sys, "argv", argv):
                asyncio.run(asyncio.wait_for(main(arguments()), 30))
            for thread in settlements:
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
            settlements.clear()

        with TemporaryDirectory(prefix="lantern-codex-image-") as tmp:
            state = Path(tmp)
            store = ChatStore(state)
            event_id = store.submit(caption, image=source)
            with patch.dict(os.environ, OPENROUTER_API_KEY="offline", OPENAI_API_KEY="offline",
                            ANTHROPIC_API_KEY="offline", LANTERN_SPOT_EVERY="1"), \
                 patch("lantern.transport._open_upstream", provider), \
                 patch("lantern.review_vendors.request_json", side_effect=reviewer), \
                 patch("lantern.__main__.serve", one_batch), patch("lantern.__main__.Thread", settlement), \
                 redirect_stderr(StringIO()):
                boot(state)
                thread_id = store.status()["thread_id"]
                self.assertTrue(thread_id)
                self.assertEqual(store.recent(1)[0].text, "Received.")
                store.submit(followup)
                boot(state)
                self.assertEqual(store.status()["thread_id"], thread_id)
                self.assertEqual(store.recent(1)[0].text, "Received.")
            rollout, = (state / "codex/sessions").rglob("rollout-*.jsonl")
            saved = rollout.read_text(encoding="utf-8")
            self.assertIn(image_reference(event_id, source.sha256), saved)
            self.assertNotIn(encoded, saved)

        self.assertEqual(len(requests), 2)
        for index, request in enumerate(requests):
            users = [item["content"] for item in request["input"] if item.get("role") == "user"]
            with_image, = [parts for parts in users if any(part.get("type") == "input_image" for part in parts)]
            self.assertTrue(any(caption in part.get("text", "") for part in with_image))
            images = [part["image_url"] for part in with_image if part["type"] == "input_image"]
            self.assertEqual(images, ["data:image/png;base64," + encoded])
            texts = [part["text"] for item in request["input"] for part in item.get("content", [])
                     if isinstance(part, dict) and "text" in part]
            self.assertNotIn(encoded, "".join(texts))
            self.assertEqual(any(followup in text for text in texts), index == 1)
        self.assertEqual(len(screens), 2)
        for parts in screens:
            self.assertTrue(any(part.get("image_url") == "data:image/png;base64," + encoded for part in parts))
            self.assertNotIn(encoded, "".join(part.get("text", "") for part in parts))
