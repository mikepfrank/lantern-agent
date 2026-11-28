from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import multiprocessing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from lantern.attachments import parse_image
from lantern.chat_store import ChatStore
from lantern.file_grants import FileGrants
from lantern.scheduler import DailyBudgetHold
from test_attachments import png, upload


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.store = ChatStore(self.state)

    def test_approval_is_queued_behind_operator_and_obeys_budget(self):
        # A pending scheduled input can predate the approval if the operator disabled scheduling.
        with self.store._connect(write=True) as db:
            reminder = self.store._append(db, "system", "A pending continuation")
            db.execute("INSERT INTO inbox(event_id) VALUES(?)", (reminder,))
        grants = FileGrants(self.state)
        request = grants.propose("Science", "Spectra", [
            {"filename": "table.csv", "url": "https://example.org/table.csv"}])
        grants.decide(request["id"], "allow_standing", room=self.store)
        approval = self.store.recent(1)[0]
        human = self.store.submit(approval.text)  # The text alone confers no Service provenance.
        batch = self.store.claim(include_scheduled=False)
        self.assertEqual([human], [e.id for e in batch.messages])
        self.assertFalse(batch.import_approval)
        self.store.complete(batch.id, "Responded to the operator")

        now = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)
        self.store.clock = lambda: now
        self.assertIsNone(self.store.claim(budget_check=lambda: False, include_scheduled=False))
        now += timedelta(days=1)
        approved = self.store.claim(budget_check=lambda: True, include_scheduled=False)
        self.assertEqual((approval,), approved.messages)
        self.assertTrue(approved.import_approval)
        self.store.complete(approved.id, "Got the grant")
        self.assertIsNone(self.store.claim(include_scheduled=False))
        previous = self.store.claim()
        self.assertEqual([reminder], [e.id for e in previous.messages])
        self.assertFalse(previous.import_approval)

    def test_compaction_notice_is_durable_timestamped_and_idempotent(self):
        start = self.store.submit("Time test")
        batch = self.store.claim().id
        self.store.bind_thread(batch, "native")
        with self.store._connect(write=True) as db:
            db.execute("DROP TABLE compaction_items")  # Current room predates compaction notices.
        peer = ChatStore(self.state)
        self.assertIsNone(peer.latest_compaction("native"))
        self.assertIsNone(peer.record_compaction(batch, "native", "first", "started"))
        self.assertIs(self.store.status()["compacting"], True)
        self.assertEqual([], self.store.events(after=start))
        completed = datetime(2026, 11, 27, 17, 18, 7, 682000, tzinfo=timezone.utc)
        timestamp = int(completed.timestamp() * 1000)
        with ThreadPoolExecutor(max_workers=3) as pool:
            receipts = list(pool.map(lambda _: peer.record_compaction(batch, "native", "first", "completed", timestamp), range(6)))
        self.assertEqual(len(set(receipts)), 1)
        event, = self.store.events(after=start)
        self.assertEqual((event.id, event.role, event.at), (receipts[0], "system", completed.isoformat()))
        self.assertIn("2026-11-27 11:18:07 AM CST", event.text)
        self.assertEqual(peer.latest_compaction("native"), event)
        self.assertIsNone(peer.latest_compaction("unrelated"))
        self.assertIsNone(peer.record_compaction(batch, "native", "first", "started"))
        self.assertIs(peer.status()["compacting"], False)
        self.assertEqual(peer.status()["queued"], 0)
        self.assertEqual(peer.claim_steer(batch), ())
        self.store.complete(batch, "Done.")
        self.assertIsNone(self.store.claim())
        self.assertEqual(ChatStore(self.state).record_compaction(batch, "native", "first", "completed"), event.id)

    def test_compaction_scope_recovery_and_missing_start(self):
        self.store.submit("First")
        first = self.store.claim().id
        self.store.bind_thread(first, "room")
        self.assertIsNone(self.store.record_compaction(first, "wrong-room", "item", "started"))
        self.assertIs(self.store.status()["compacting"], False)
        self.store.record_compaction(first, "room", "item", "started")
        self.assertEqual(self.store.recover(), 1)
        self.assertIs(self.store.status()["compacting"], False)
        self.store.submit("Next")
        second = self.store.claim().id
        self.assertIsNone(self.store.record_compaction(first, "room", "item", "completed"))
        self.assertIsNone(self.store.record_compaction(second, "wrong-room", "item", "completed"))
        self.assertIsNone(self.store.latest_compaction("room"))
        now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
        self.store.clock = lambda: now
        event = self.store.record_compaction(second, "room", "item", "completed", 10**99)
        self.assertEqual(self.store.latest_compaction("room").id, event)
        self.assertEqual(self.store.latest_compaction("room").at, now.isoformat())
        self.assertIn("07:00:00 AM CDT", self.store.latest_compaction("room").text)
        self.store.record_compaction(second, "room", "another", "started")
        self.assertIs(self.store.status()["compacting"], True)
        self.store.defer_for_budget(second, DailyBudgetHold(dispatched=False))
        self.assertIs(self.store.status()["compacting"], False)
        self.assertEqual(self.store.latest_compaction("room").id, event)

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

    def test_reply_blocks_are_visible_once_per_item_while_the_batch_is_active(self):
        self.store.submit("Check")
        batch = self.store.claim()
        self.assertIsNone(self.store.append_reply_block(batch.id, "empty", " \n "))
        with self.assertRaisesRegex(ValueError, "empty chat reply"):
            self.store.complete(batch.id, "")
        body = "    print(42)"
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: self.store.append_reply_block(batch.id, "msg1", "\n" + body + "\n"),
                                     range(8)))
        self.assertEqual(len(set(receipts)), 1)
        peer = ChatStore(self.state)
        self.assertEqual(batch.id, peer.status()["active_batch"])
        self.assertEqual([body], [e.text for e in peer.events(after=batch.messages[0].id)])
        self.assertEqual(receipts[0], peer.append_reply_block(batch.id, "msg1", body))
        with self.assertRaisesRegex(ValueError, "different text"):
            peer.append_reply_block(batch.id, "msg1", "Changed")
        # Both identifier namespaces and distinct native messages remain independent.
        tool_notice = peer.notify(batch.id, "Working", "msg1")
        final = peer.append_reply_block(batch.id, "msg2", " \nFinished. \n")
        self.assertEqual(final, peer.complete(batch.id, ""))
        self.assertEqual(final, peer.complete(batch.id, ""))
        self.assertEqual(final, peer.append_reply_block(batch.id, "msg2", "Finished."))
        self.assertEqual(["Working", "Finished."], [e.text for e in peer.events(after=receipts[0])])
        self.assertLess(tool_notice, final)
        with self.assertRaisesRegex(ValueError, "no longer active"):
            peer.append_reply_block(batch.id, "late", "Late")
        self.store.submit("Next")
        next_batch = self.store.claim()
        self.assertNotEqual(receipts[0], peer.append_reply_block(next_batch.id, "msg1", body))
        last = peer.notify(next_batch.id, "Done", "call1")
        self.assertEqual(last, peer.complete(next_batch.id, ""))

    def test_interrupt_keeps_published_blocks_then_records_failure(self):
        self.store.submit("Work")
        batch = self.store.claim()
        first = self.store.append_reply_block(batch.id, "msg1", "Checking.")
        peer = ChatStore(self.state)
        self.assertEqual(1, peer.recover())
        self.assertEqual(0, peer.recover())
        block, interruption = peer.events(after=batch.messages[0].id)
        self.assertEqual((block.id, block.role, block.text), (first, "lantern", "Checking."))
        self.assertEqual(interruption.role, "system")
        self.assertIn("interrupted", interruption.text)
        self.assertEqual(first, peer.append_reply_block(batch.id, "msg1", "Checking."))
        with self.assertRaisesRegex(ValueError, "different outcome"):
            peer.complete(batch.id, "")

    def test_finished_at_migrates_without_rewriting_old_turns(self):
        self.store.submit("Before upgrade")
        batch = self.store.claim()
        self.store.complete(batch.id, "Old reply")
        with self.store._connect(write=True) as db:
            db.execute("ALTER TABLE batches DROP COLUMN finished_at")
            db.execute("DROP TABLE reply_blocks")
        store = ChatStore(self.state)
        store.submit("After upgrade")
        newer = store.claim()
        store.append_reply_block(newer.id, "msg1", "New reply")
        store.complete(newer.id, "")
        with store._connect() as db:
            older, current = db.execute("SELECT finished_at FROM batches ORDER BY id")
            self.assertIsNone(older[0])
            self.assertIsNotNone(datetime.fromisoformat(current[0]).utcoffset())

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
