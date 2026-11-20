"""Native Codex steering through the real loopback gateway and an offline provider."""

import asyncio
import base64
from contextlib import contextmanager, redirect_stderr, suppress
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lantern.__main__ import main
from lantern.attachments import parse_image
from lantern.budget import Budget
from lantern.chat_store import ChatStore
from lantern.codex_session import _Peer
from lantern.transport import OpenRouterGateway
from test_attachments import png, upload
from test_codex_smoke import FixtureStream, reply


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "opt in with LANTERN_TEST_CODEX=1")
class NativeSteering(unittest.TestCase):
    def test_operator_can_steer_one_in_flight_turn(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native codex executable")
        prompt = "Start the calculation. Reference River-190."
        followup = "Use the amended figure instead. Reference Orchard-843."
        drawing = parse_image(upload(png()))
        encoded = base64.b64encode(drawing.data).decode("ascii")
        first_inference = Event()
        acknowledged = Event()
        release_old_response = Event()
        settlement_finished = Event()
        settlement_timed_out = Event()
        requests, sent, started, finished = [], [], [], []
        reports = []
        transmitted_images = []
        steer_ids = set()
        send, read = _Peer.send, _Peer.read

        async def record_send(peer, message):
            if message.get("method") in ("turn/start", "turn/steer"):
                sent.append(message)
                if message["method"] == "turn/steer":
                    steer_ids.add(message["id"])
            await send(peer, message)

        async def record_read(peer):
            message = await read(peer)
            if message.get("id") in steer_ids and "result" in message:
                acknowledged.set()
            if message.get("method") == "turn/started":
                started.append(message["params"]["turn"]["id"])
            elif message.get("method") == "turn/completed":
                finished.append(message["params"]["turn"]["id"])
            return message

        class SlowFirstResponse(FixtureStream):
            def __init__(self, data):
                super().__init__(data)
                self.end_of_first_frame = data.index(b"\n\n") + 2

            def readline(self, *args):
                # The HTTP response and first SSE frame have already reached
                # Codex. The operator must see the amended answer even while
                # this superseded provider request is still awaiting billing.
                if self.tell() == self.end_of_first_frame:
                    first_inference.set()
                    if not release_old_response.wait(15):
                        settlement_timed_out.set()
                        raise TimeoutError("The worker waited for obsolete provider billing before publishing")
                return super().readline(*args)

        @contextmanager
        def provider(body, headers):
            payload = json.loads(body)
            self.assertIn("Michael P. Frank", payload["instructions"])
            users = [part["text"] for item in payload["input"] if item.get("role") == "user"
                     for part in item["content"] if part.get("type") == "input_text"]
            images = [(item["content"], part["image_url"]) for item in payload["input"] if item.get("role") == "user"
                      for part in item["content"] if part.get("type") == "input_image"]
            transmitted_images.append(images)
            requests.append(users)
            serial = len(requests)
            answer = "I used Orchard-843." if any(followup in user for user in users) else "I used River-190."
            item = {"type": "message", "role": "assistant", "id": f"msg_{serial}", "status": "completed",
                    "content": [{"type": "output_text", "annotations": [], "text": answer}]}
            stream = reply(serial, item)
            yield SlowFirstResponse(stream.getvalue()) if serial == 1 else stream

        class ObservedGateway(OpenRouterGateway):
            def __exit__(self, *exc):
                super().__exit__(*exc)
                reports.extend(self.evidence)
                settlement_finished.set()

        def fake_review(vendor, key, body):
            self.assertEqual(vendor, "luna")
            report = json.dumps({"escalate": False, "evidence": "", "summary": "An operator updates a figure."})
            return {"model": json.loads(body)["model"], "status": "completed",
                    "usage": {"input_tokens": 50, "output_tokens": 20},
                    "output": [{"type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": report}]}]}

        async def exercise(state):
            store = ChatStore(state)
            usage = Budget(state / "usage.sqlite", Decimal("1"))
            store.submit(prompt)
            args = SimpleNamespace(command="worker", state=state, codex=binary, persona=None, route="fastest",
                                   private_lightsail_cli=True, heritage_dir=None)
            worker = asyncio.create_task(main(args))
            try:
                async with asyncio.timeout(12):
                    while not first_inference.is_set():
                        if worker.done():
                            await worker
                        await asyncio.sleep(.025)
                    store.submit(followup, image=drawing)
                    while not any(event.role in ("lantern", "system") for event in store.events()):
                        if worker.done():
                            await worker
                        await asyncio.sleep(.025)
                self.assertEqual([(event.role, event.text) for event in store.events()], [
                    ("operator", prompt), ("operator", followup), ("lantern", "I used Orchard-843.")])
                self.assertFalse(settlement_timed_out.is_set())
                self.assertFalse(settlement_finished.is_set())
                self.assertEqual(store.status()["queued"], 0)
                self.assertIsNone(store.status()["active_batch"])
                before = usage.today()
                self.assertEqual((before["requests"], Decimal(before["spent_usd"])), (2, Decimal("0.00001")))
                self.assertGreater(Decimal(before["reserved_usd"]), 0)
            finally:
                release_old_response.set()
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
                if requests:
                    self.assertTrue(await asyncio.to_thread(settlement_finished.wait, 10),
                                    "The gateway failed to finish accounting")
            after = usage.today()
            self.assertEqual((after["requests"], Decimal(after["spent_usd"]), Decimal(after["reserved_usd"])),
                             (2, Decimal("0.00002"), 0))
            return store.status()["thread_id"]

        with TemporaryDirectory(prefix="lantern-native-steer-") as tmp:
            with patch.dict(os.environ, OPENROUTER_API_KEY="offline", OPENAI_API_KEY="offline",
                            ANTHROPIC_API_KEY="offline", LANTERN_REVIEW_DAILY_USD="5", LANTERN_SPOT_EVERY="12"), \
                 patch("lantern.transport._open_upstream", provider), \
                 patch("lantern.review_vendors.request_json", side_effect=fake_review), \
                 patch.object(_Peer, "send", record_send), patch.object(_Peer, "read", record_read), \
                 patch("lantern.__main__.OpenRouterGateway", ObservedGateway), redirect_stderr(StringIO()):
                thread_id = asyncio.run(exercise(Path(tmp)))

        self.assertEqual([m["method"] for m in sent], ["turn/start", "turn/steer"])
        self.assertEqual(len(started), 1)
        self.assertEqual(finished, started)
        self.assertEqual(sent[1]["params"]["expectedTurnId"], started[0])
        self.assertEqual(sent[1]["params"]["threadId"], thread_id)
        self.assertEqual([part["type"] for part in sent[1]["params"]["input"]], ["text", "image"])
        self.assertTrue(acknowledged.is_set())
        self.assertEqual(len(requests), 2, reports)
        self.assertEqual([sum(text.count(prompt) for text in users) for users in requests], [1, 1])
        self.assertEqual([sum(text.count(followup) for text in users) for users in requests], [0, 1])
        self.assertTrue(all(encoded not in text for users in requests for text in users))
        self.assertEqual(transmitted_images[0], [])
        amended, = transmitted_images[1]
        parts, image_url = amended
        self.assertTrue(any(followup in part.get("text", "") for part in parts))
        self.assertEqual(image_url, "data:image/png;base64," + encoded)
        self.assertEqual([entry["status"] for entry in reports], ["completed", "completed"])
