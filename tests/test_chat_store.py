from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import multiprocessing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.attachments import parse_image
from lantern.chat_store import ChatStore
from test_attachments import png, upload


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.store = ChatStore(self.state)

    def test_routing_uses_startup_default_until_chosen_and_survives_replay(self):
        for default in ("fastest", "cheapest", "default"):
            self.assertEqual(default, self.store.provider_route(default))
        old = self.store.set_provider_route("fastest", call_id="first")
        self.assertEqual({"route": "fastest", "sort": "throughput"}, old)
        self.assertEqual("fastest", self.store.provider_route("cheapest"))
        current = self.store.set_provider_route("cheapest", call_id="second")
        self.assertEqual({"route": "cheapest", "sort": "price"}, current)
        reopened = ChatStore(self.state)
        self.assertEqual(old, reopened.set_provider_route("fastest", call_id="first"))
        with self.assertRaisesRegex(ValueError, "different routing choice"):
            reopened.set_provider_route("cheapest", call_id="first")
        self.assertEqual("cheapest", reopened.provider_route("fastest"))
        self.assertEqual(reopened.set_provider_route("default", call_id="balanced"), {"route": "default", "sort": None})
        self.assertEqual(ChatStore(self.state).provider_route("fastest"), "default")

    def test_invalid_route_or_receipt_preserves_room_choice(self):
        self.store.set_provider_route("cheapest", call_id="good")
        for invalid in ("price", "", "fastest ", True, None, []):
            with self.subTest(route=invalid), self.assertRaises(ValueError):
                self.store.set_provider_route(invalid, call_id="unused")
        for invalid in ("", " ", None, 0):
            with self.subTest(call=invalid), self.assertRaises(ValueError):
                self.store.set_provider_route("fastest", call_id=invalid)
        self.assertEqual("cheapest", self.store.provider_route("fastest"))
        self.assertEqual("fastest", self.store.set_provider_route("fastest", call_id="unused")["route"])

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

    def test_ordered_images_survive_reopen_and_retries(self):
        red, blue = [parse_image(upload(png(pixels=p))) for p in (b"\0\xff\0\0", b"\0\0\0\xff")]
        selected = [red, blue, red]
        receipt = self.store.submit("Three screenshots", "upload", images=selected)
        reopened = ChatStore(self.state)
        for event in (reopened.events()[0], reopened.recent()[0], reopened.claim().messages[0]):
            self.assertEqual([i.sha256 for i in selected], [i["sha256"] for i in event.images])
            self.assertEqual(event.image, event.images[0])
        self.assertEqual([i.data for i in selected], [reopened.image_data(receipt, n) for n in range(3)])
        self.assertIsNone(reopened.image_data(receipt, 3))
        self.assertEqual(blue.data, reopened.image_data_by_digest(receipt, blue.sha256))
        other = reopened.submit("One", image=red)
        self.assertIsNone(reopened.image_data_by_digest(other, blue.sha256))
        self.assertEqual(receipt, reopened.submit("Three screenshots", "upload", images=selected))
        for different in ([blue, red, red], [red, blue], [red, blue, blue], []):
            with self.subTest(different=[i.sha256 for i in different]), self.assertRaises(ValueError):
                reopened.submit("Three screenshots", "upload", images=different)

    def test_existing_single_image_database_upgrades_in_place(self):
        image = parse_image(upload(png()))
        receipt = self.store.submit("", "legacy", image=image)
        with self.store._connect(write=True) as db:
            db.execute("DROP TABLE extra_message_images")  # Original single-image schema.
        reopened = ChatStore(self.state)
        self.assertEqual(receipt, reopened.submit("", "legacy", images=[image]))
        old, = reopened.recent()
        self.assertEqual((old.image,), old.images)
        self.assertEqual(image.data, reopened.image_data(receipt))
        self.assertEqual(image.data, reopened.image_data_by_digest(receipt, image.sha256))

    def test_multi_image_admission_keeps_message_together_across_steer(self):
        image = parse_image(upload(png()))
        first = self.store.submit("two", images=[image, image])
        batch = self.store.claim()
        self.assertEqual([first], [m.id for m in batch.messages])
        fits = self.store.submit("two more", images=[image, image])
        self.assertEqual([fits], [m.id for m in self.store.claim_steer(batch.id)])
        self.store.release_steer(batch.id, [fits])
        self.assertEqual([fits], [m.id for m in self.store.claim_steer(batch.id)])
        held = self.store.submit("", images=[image] * 3)
        text = self.store.submit("Caption follow-up")
        self.assertEqual((), self.store.claim_steer(batch.id))
        self.store.complete(batch.id, "Saw the four images")
        next_batch = self.store.claim()
        self.assertEqual([held, text], [m.id for m in next_batch.messages])
        later = self.store.submit("needs two slots", images=[image] * 2)
        self.assertEqual((), self.store.claim_steer(next_batch.id))
        self.store.complete(next_batch.id, "Saw the three images")
        self.assertEqual([later], [m.id for m in self.store.claim().messages])

    def test_rejected_multi_image_does_not_store_part_or_consume_receipt(self):
        image = parse_image(upload(png()))
        for invalid in ([image] * 5, [image, "invalid"]):
            with self.assertRaises(ValueError):
                self.store.submit("", "upload", images=invalid)
        with self.store._connect(write=True) as db:
            db.execute("""CREATE TRIGGER simulate_failure BEFORE INSERT ON extra_message_images
                WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT,'simulated failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.submit("", "upload", images=[image] * 3)
        self.assertEqual([], self.store.events())
        self.assertEqual(0, self.store.status()["queued"])
        self.store.submit("", "upload", images=[image] * 2)
        self.assertEqual(2, len(self.store.recent()[0].images))

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
