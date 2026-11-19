"""Native Codex steering through the real loopback gateway and an offline provider."""

import asyncio
from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from lantern.codex_session import _Peer, run_turn, SteeringInput
from lantern.transport import MODEL, OpenRouterGateway
from test_codex_smoke import FixtureStream, reply


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "opt in with LANTERN_TEST_CODEX=1")
class NativeSteering(unittest.TestCase):
    def test_operator_can_steer_one_in_flight_turn(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native codex executable")
        prompt = "Start the calculation. Reference River-190."
        followup = "Use the amended figure instead. Reference Orchard-843."
        first_inference = Event()
        acknowledged = Event()
        claimed, rejected = [], []
        requests, sent, started, finished = [], [], [], []
        reservations, settlements = [], {}
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
                # Codex. Only release the rest once *native Codex* accepts input.
                if self.tell() == self.end_of_first_frame:
                    first_inference.set()
                    if not acknowledged.wait(10):
                        raise TimeoutError("No turn/steer acknowledgement during first response")
                return super().readline(*args)

        @contextmanager
        def provider(body, headers):
            users = [part["text"] for item in json.loads(body)["input"] if item.get("role") == "user"
                     for part in item["content"] if part.get("type") == "input_text"]
            requests.append(users)
            serial = len(requests)
            answer = "I used Orchard-843." if any(followup in user for user in users) else "I used River-190."
            item = {"type": "message", "role": "assistant", "id": f"msg_{serial}", "status": "completed",
                    "content": [{"type": "output_text", "annotations": [], "text": answer}]}
            stream = reply(serial, item)
            yield SlowFirstResponse(stream.getvalue()) if serial == 1 else stream

        def take_input():
            if first_inference.is_set() and not claimed:
                claimed.append(2)
                return SteeringInput((2,), followup)
            return None

        def reserve(body):
            key = len(reservations) + 1
            reservations.append(key)
            return key

        def settle(key, cost):
            if key in settlements:
                raise AssertionError("A provider request was charged twice")
            settlements[key] = cost

        with TemporaryDirectory(prefix="lantern-native-steer-") as tmp:
            home, workspace = Path(tmp) / "codex", Path(tmp) / "workspace"
            home.mkdir()
            workspace.mkdir()
            with patch("lantern.transport._open_upstream", provider), \
                 patch.object(_Peer, "send", record_send), patch.object(_Peer, "read", record_read), \
                 OpenRouterGateway("offline", route="fastest", tools=[], reserve=reserve,
                                   settle=settle, observe=lambda body: None,
                                   unregistered=self.fail) as gateway:
                result = asyncio.run(run_turn(
                    codex_bin=binary, codex_home=home, workspace=workspace, model=MODEL, provider="lantern",
                    persona="You are Lantern.", prompt=prompt, tools=[], dispatch=self.fail,
                    env={"LANTERN_GATEWAY_TOKEN": gateway.token}, base_url=gateway.base_url,
                    take_input=take_input, return_input=rejected.extend, timeout=30,
                ))

        self.assertEqual([m["method"] for m in sent], ["turn/start", "turn/steer"])
        self.assertEqual(started, [result.turn_id])
        self.assertEqual(finished, started)
        self.assertEqual(sent[1]["params"]["expectedTurnId"], result.turn_id)
        self.assertEqual(sent[1]["params"]["threadId"], result.thread_id)
        self.assertTrue(acknowledged.is_set())
        self.assertEqual(claimed, [2])
        self.assertEqual(rejected, [])
        self.assertEqual(len(requests), 2, gateway.evidence)
        self.assertEqual([sum(text.count(prompt) for text in users) for users in requests], [1, 1])
        self.assertEqual([sum(text.count(followup) for text in users) for users in requests], [0, 1])
        self.assertEqual(result.text, "I used Orchard-843.")
        self.assertEqual([entry["status"] for entry in gateway.evidence], ["completed", "completed"])
        self.assertEqual(reservations, [1, 2])
        self.assertEqual(settlements, {1: Decimal("0.00001"), 2: Decimal("0.00001")})
