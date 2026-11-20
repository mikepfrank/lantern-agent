from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lantern.chat_store import ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.store = ChatStore(self.state)

    def test_receipt_is_idempotent_under_concurrent_writers(self):
        def send(_):
            return ChatStore(self.state).submit("first\nsecond", "client-receipt")

        with ThreadPoolExecutor(max_workers=5) as pool:
            receipts = list(pool.map(send, range(10)))
        self.assertEqual(1, len(set(receipts)))
        with self.assertRaisesRegex(ValueError, "different text"):
            self.store.submit("different", "client-receipt")
        self.assertEqual("first\nsecond", self.store.recent()[0].text)
        self.assertEqual(1, self.store.status()["queued"])

    def test_turn_and_thread_survive_process_restart(self):
        first = self.store.submit("First")
        second = self.store.submit("Second")
        batch = self.store.claim(limit=2)
        self.assertEqual([first, second], [m.id for m in batch.messages])
        self.assertIsNone(self.store.claim())
        self.assertIsNone(batch.thread_id)
        self.store.bind_thread(batch.id, "native-thread")
        third = self.store.submit("Third")
        self.assertEqual(1, ChatStore(self.state).status()["queued"])
        reply = self.store.complete(batch.id, "Reply to first two")
        self.assertEqual(reply, self.store.complete(batch.id, "Reply to first two"))
        with self.assertRaisesRegex(ValueError, "different outcome"):
            self.store.fail(batch.id, "late failure")
        fresh = ChatStore(self.state)
        self.assertEqual(0, fresh.recover())
        next_turn = fresh.claim()
        self.assertEqual("native-thread", next_turn.thread_id)
        self.assertEqual([third], [m.id for m in next_turn.messages])
        with self.assertRaisesRegex(ValueError, "differs"):
            fresh.bind_thread(next_turn.id, "wrong-thread")
        failure = fresh.fail(next_turn.id, "Example failure")
        self.assertEqual(failure, fresh.fail(next_turn.id, "Example failure"))
        self.assertIsNone(fresh.claim())
        self.assertTrue(fresh.events(after=reply)[0].text.endswith("Example failure"))

    def test_interrupt_does_not_replay_uncertain_steering_or_tools(self):
        initial = self.store.submit("Start")
        batch = self.store.claim()
        self.store.bind_thread(batch.id, "native")
        steered = self.store.submit("Mid-turn")
        self.assertEqual([steered], [e.id for e in self.store.claim_steer(batch.id)])
        late = self.store.submit("Late, with definitive Codex rejection")
        self.store.claim_steer(batch.id)
        with self.assertRaisesRegex(ValueError, "not steering"):
            self.store.release_steer(batch.id, [late, initial])
        self.assertEqual(0, self.store.status()["queued"])  # Failed release rolled back.
        self.store.release_steer(batch.id, [late])
        queued = self.store.submit("Not claimed")
        store = ChatStore(self.state)
        self.assertEqual(1, store.recover())
        self.assertEqual(0, store.recover())
        self.assertEqual(2, store.status()["queued"])
        notice, = store.events(after=queued)
        self.assertEqual("system", notice.role)
        self.assertIn(f"operator messages {initial}–{steered}", notice.text)
        self.assertIn("interrupted", notice.text)
        self.assertEqual([late, queued], [e.id for e in store.claim().messages])
        with self.assertRaisesRegex(ValueError, "different outcome"):
            self.store.complete(batch.id, "stale worker reply")

    def test_notification_persists_before_final_and_is_not_replayed(self):
        self.store.submit("Please notify me while working")
        batch = self.store.claim()
        notice = self.store.notify(batch.id, "Still working.", "tool-receipt")
        peer = ChatStore(self.state)
        self.assertEqual("Still working.", peer.recent(1)[0].text)
        self.assertEqual(batch.id, peer.status()["active_batch"])
        self.assertEqual(notice, peer.notify(batch.id, "Still working.", "tool-receipt"))
        with self.assertRaisesRegex(ValueError, "different text"):
            peer.notify(batch.id, "Changed", "tool-receipt")
        self.assertEqual(notice, peer.complete(batch.id, ""))
        self.assertEqual(notice, peer.complete(batch.id, ""))
        self.assertEqual(notice, peer.notify(batch.id, "Still working.", "tool-receipt"))
        self.assertEqual([], peer.events(after=notice))
        with self.assertRaisesRegex(ValueError, "no longer active"):
            peer.notify(batch.id, "stale", "different-call")

    def test_padded_notification_is_stored_once_and_old_receipts_remain_valid(self):
        self.store.submit("Start")
        batch = self.store.claim()
        notice = self.store.notify(batch.id, "\r\n  Result.\n\n", "result")
        self.assertEqual(self.store.recent(1)[0].text, "Result.")
        self.assertEqual(notice, self.store.notify(batch.id, "Result.", "result"))
        # Simulate a still-present receipt written by the pre-trimming worker.
        old = "\n\n\tprint(42)\n\n"
        with self.store._connect(write=True) as db:
            db.execute("UPDATE events SET text=? WHERE id=?", (old, notice))
        self.assertEqual(notice, self.store.notify(batch.id, "\tprint(42)", "result"))
        self.assertEqual(self.store.recent(1)[0].text, old)
        with self.assertRaisesRegex(ValueError, "different text"):
            self.store.notify(batch.id, "print(42)", "result")

    def test_transcript_and_read_cursor_are_monotonic(self):
        first = self.store.submit("1")
        second = self.store.submit("2")
        third = self.store.submit("3")
        self.assertIsNone(self.store.read_cursor())
        self.store.mark_read(0)
        self.assertEqual(0, self.store.read_cursor())
        self.store.mark_read(second)
        peer = ChatStore(self.state)
        peer.mark_read(first)
        self.assertEqual(second, peer.read_cursor())
        self.assertEqual([first, second], [e.id for e in peer.events(limit=2)])
        self.assertEqual([third], [e.id for e in peer.events(after=second)])
        self.assertEqual([second, third], [e.id for e in peer.recent(limit=2)])
        self.assertTrue(all(datetime.fromisoformat(e.at).utcoffset() == timedelta(0)
                            for e in peer.recent()))
        with self.assertRaises(ValueError):
            peer.mark_read(third + 1)

    def test_other_process_can_send_while_turn_is_claimed(self):
        self.store.submit("Kickoff")
        batch = self.store.claim()
        process = multiprocessing.get_context("spawn").Process(target=process_submit,
                                                              args=(self.state, "Follow up"))
        process.start()
        try:
            process.join(10)
            self.assertEqual(0, process.exitcode)
        finally:
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
        self.assertEqual(["Follow up"], [e.text for e in self.store.claim_steer(batch.id)])


def process_submit(state, text):
    ChatStore(state).submit(text)
