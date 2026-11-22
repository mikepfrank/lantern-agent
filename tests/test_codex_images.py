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
    def test_ordered_room_images_and_captions_survive_a_native_process_restart(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native Codex executable")
        screenshots = [parse_image(upload(png(pixels=b"\0" + bytes(rgb))))
                       for rgb in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]]
        # Reattaching the green screenshot exercises the identical-digest case.
        followup_images = [screenshots[1], screenshots[3]]
        caption = "Describe these three private diagrams in order."
        followup = "Compare the next two to the earlier diagrams."
        def pixel_urls(images):
            return ["data:image/png;base64," + base64.b64encode(image.data).decode("ascii") for image in images]
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
            first_id = store.submit(caption, images=screenshots[:3])
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
                second_id = store.submit(followup, images=followup_images)
                boot(state)
                self.assertEqual(store.status()["thread_id"], thread_id)
                self.assertEqual(store.recent(1)[0].text, "Received.")
            rollout, = (state / "codex/sessions").rglob("rollout-*.jsonl")
            saved = rollout.read_text(encoding="utf-8")
            for event_id, images in [(first_id, screenshots[:3]), (second_id, followup_images)]:
                for image in images:
                    self.assertIn(image_reference(event_id, image.sha256), saved)
                for url in pixel_urls(images):
                    self.assertNotIn(url, saved)

        self.assertEqual(len(requests), 2)
        for index, request in enumerate(requests):
            users = [item["content"] for item in request["input"] if item.get("role") == "user"]
            with_images = [parts for parts in users if any(part.get("type") == "input_image" for part in parts)]
            expected = [screenshots[:3]] if index == 0 else [screenshots[1:3], followup_images]
            self.assertEqual([[p["image_url"] for p in parts if p["type"] == "input_image"] for parts in with_images],
                             [pixel_urls(images) for images in expected])
            self.assertTrue(any(caption in part.get("text", "") for part in with_images[0]))
            if index == 1:
                self.assertTrue(any(followup in part.get("text", "") for part in with_images[1]))
                self.assertTrue(any("archived" in part.get("text", "") for part in with_images[0]))
            texts = [part["text"] for item in request["input"] for part in item.get("content", [])
                     if isinstance(part, dict) and "text" in part]
            for url in pixel_urls(screenshots):
                self.assertNotIn(url, "".join(texts))
            self.assertEqual(any(followup in text for text in texts), index == 1)
        self.assertEqual(len(screens), 2)
        for parts, images in zip(screens, [screenshots[:3], screenshots[1:3] + followup_images]):
            self.assertEqual([part["image_url"] for part in parts if part["type"] == "input_image"], pixel_urls(images))
            for url in pixel_urls(images):
                self.assertNotIn(url, parts[0]["text"])
