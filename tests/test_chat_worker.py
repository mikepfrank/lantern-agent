import asyncio
import json
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest

from lantern.chat_store import ChatStore
from lantern.chat_worker import WorkerBusy, process_batch, serve, worker_lock, worker_online


def run_worker(state, block):
    """Exercise the real worker loop in a killable process with an offline model."""
    async def turn(prompt, route, thread_id, *, on_thread, notify, **callbacks):
        with (state / "model-calls.jsonl").open("a", encoding="utf-8") as f:
            print(json.dumps([prompt, route, thread_id]), file=f)
        on_thread("test-native-thread")
        if block:
            notify("notice", {"text": "Working on the first input"})
            await asyncio.Event().wait()
        return SimpleNamespace(text="Answered the new input"), []

    asyncio.run(serve(state, ChatStore(state), turn, "fastest"))


class ChatWorkerTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.store = ChatStore(self.state)

    def start_worker(self, *, block):
        proc = multiprocessing.get_context("spawn").Process(target=run_worker, args=(self.state, block))
        proc.start()

        def stop():
            if proc.is_alive():
                proc.kill()
            proc.join(10)
            self.assertFalse(proc.is_alive())
            proc.close()

        self.addCleanup(stop)
        return proc

    def wait_for_message(self, proc, message):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(e.text == message for e in self.store.events()):
                return
            self.assertTrue(proc.is_alive(), f"Worker exited early with {proc.exitcode}")
            time.sleep(.03)
        self.fail(f"Timed out waiting for {message!r}")

    def test_killed_worker_releases_lock_and_resumes_fresh_input_without_replay(self):
        self.assertFalse(worker_online(self.state))
        initial = self.store.submit("First input")
        first = self.start_worker(block=True)
        self.wait_for_message(first, "Working on the first input")
        self.assertTrue(worker_online(self.state))
        with self.assertRaises(WorkerBusy), worker_lock(self.state):
            self.fail("Second worker acquired an occupied lock")
        self.store.submit("New input")
        self.assertEqual(1, self.store.status()["queued"])

        first.kill()
        first.join(10)
        self.assertFalse(first.is_alive())
        self.assertFalse(worker_online(self.state))
        self.assertIsNotNone(self.store.status()["active_batch"])

        second = self.start_worker(block=False)
        self.wait_for_message(second, "Answered the new input")
        self.assertEqual({"active_batch": None, "queued": 0, "thread_id": "test-native-thread"},
                         self.store.status())
        calls = (self.state / "model-calls.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([["First input", "fastest", None], ["New input", "fastest", "test-native-thread"]],
                         [json.loads(line) for line in calls])
        history = self.store.events()
        self.assertEqual(["operator", "lantern", "operator", "system", "lantern"], [e.role for e in history])
        self.assertIn(f"interrupted (operator messages {initial})", history[3].text)
        self.assertEqual(0, self.store.recover())

    def test_batch_steering_and_deliberate_notification_without_final_text(self):
        self.store.submit("Initial")
        self.store.submit("Before dispatch")
        batch = self.store.claim()

        async def turn(prompt, route, thread_id, *, on_thread, take_input, return_input, notify, batch_id, trigger):
            self.assertEqual((batch.id, "operator"), (batch_id, trigger))
            self.assertEqual("Initial\n\nBefore dispatch", prompt)
            on_thread("native-thread")
            self.assertIsNone(take_input())
            accepted = self.store.submit("Arrived mid-turn")
            rejected = self.store.submit("Delivered next turn")
            steering = take_input()
            self.assertEqual((accepted, rejected), steering.ids)
            self.assertEqual("Arrived mid-turn\n\nDelivered next turn", steering.text)
            return_input([rejected])
            for invalid in ({"text": " "}, {"text": "x" * 8001}, {"unexpected": "x"}, {"text": 1}):
                with self.assertRaises(ValueError):
                    notify("invalid", invalid)
            receipt = notify("intentional", {"text": "Operator update"})
            self.assertEqual({"event_id": self.store.recent()[-1].id, "delivered_to": "operator chat"}, receipt)
            self.assertEqual(receipt, notify("intentional", {"text": "Operator update"}))
            with self.assertRaises(ValueError):
                notify("intentional", {"text": "Different content"})
            return SimpleNamespace(text=""), []

        asyncio.run(process_batch(self.store, batch, turn, "cheapest"))
        events = self.store.events()
        self.assertEqual(["Operator update"], [e.text for e in events if e.role == "lantern"])
        self.assertFalse(any(e.role == "system" for e in events))
        next_batch = self.store.claim()
        self.assertEqual(["Delivered next turn"], [e.text for e in next_batch.messages])
        self.assertEqual("native-thread", next_batch.thread_id)
