import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from lantern.chat_store import ChatStore
from lantern.chat_worker import process_batch
from lantern.scheduler import DailyBudgetHold, dispatch_schedule, next_reset


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc)
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.reopen()

    def reopen(self):
        return ChatStore(self.state, clock=lambda: self.now)

    def test_cadence_survives_restart_and_coalesces_downtime(self):
        self.store.initialize_heartbeat(60)
        self.assertIsNone(self.store.claim(include_scheduled=False))
        first = self.store.claim()
        self.assertEqual("system", first.messages[0].role)
        self.assertIn("heartbeat", first.messages[0].text)
        self.store.complete(first.id, "First wake")
        self.assertIsNone(self.store.claim())
        self.now += timedelta(days=2, hours=2, minutes=17)
        store = self.reopen()
        store.initialize_heartbeat(17)
        self.assertEqual(60, store.schedule_status()["heartbeat_minutes"])
        second = store.claim()
        self.assertIsNotNone(second)
        self.assertEqual((self.now + timedelta(hours=1)).isoformat(), store.schedule_status()["next_heartbeat"])
        store.recover()
        self.assertIsNone(store.claim())
        for invalid in (-1, 2, 10081, True, "60"):
            with self.assertRaises(ValueError):
                store.configure_heartbeat(invalid)
        paused = dispatch_schedule(store, "call-a", "heartbeat", {"minutes": 0})
        self.assertIsNone(paused["next_heartbeat"])
        store.initialize_heartbeat(60)
        self.now += timedelta(days=3)
        self.assertIsNone(store.claim())
        update = store.configure_heartbeat(5, call_id="call-b")
        self.now += timedelta(minutes=2)
        self.assertEqual(update, store.configure_heartbeat(5, call_id="call-b"))
        self.assertIsNone(store.claim())
        self.now += timedelta(minutes=4)
        self.assertIsNotNone(store.claim())

    def test_due_alarms_behind_human_ahead_of_heartbeat_no_duplicate_delivery(self):
        self.store.initialize_heartbeat(60)
        alarm_input = {"at_utc": "2026-11-19T12:04-06:00", "note": 'Try a line.\n"Quoted"'}
        first = dispatch_schedule(self.store, "alarm-1", "set_alarm", alarm_input)
        second = self.store.set_alarm("2026-11-19T18:05Z", "Cancel me", call_id="alarm-2")
        self.assertEqual("cancelled", self.store.cancel_alarm(second["alarm_id"], call_id="cancel-1")["status"])
        self.assertEqual("2026-11-19T18:04:00+00:00", first["at_utc"])
        self.now += timedelta(minutes=8)
        self.assertEqual(first, dispatch_schedule(self.reopen(), "alarm-1", "set_alarm", alarm_input))
        with self.assertRaisesRegex(ValueError, "different schedule change"):
            self.store.configure_heartbeat(5, call_id="alarm-1")
        self.store.submit("Human first")
        human = self.store.claim()
        self.assertEqual(["Human first"], [event.text for event in human.messages])
        self.assertEqual((), self.store.claim_steer(human.id))
        self.store.complete(human.id, "Reply")
        alarm = self.store.claim()
        self.assertEqual("system", alarm.messages[0].role)
        self.assertIn("Scheduled alarm", alarm.messages[0].text)
        self.assertIn(r"Try a line.\n", alarm.messages[0].text)
        steered = self.store.submit("Actually, stop")
        self.assertEqual([steered], [event.id for event in self.store.claim_steer(alarm.id)])
        self.assertEqual(1, self.reopen().recover())
        self.assertIn("input events", self.store.events()[-1].text)
        self.assertEqual([], self.store.schedule_status()["pending_alarms"])
        self.assertEqual("delivered", self.store.cancel_alarm(first["alarm_id"])["status"])
        self.assertIn("heartbeat", self.store.claim().messages[0].text)

    def test_alarm_validation_and_pagination(self):
        for invalid in ("2026-11-20", "invalid", "2026-11-19T18:00:00Z"):
            with self.assertRaises(ValueError):
                self.store.set_alarm(invalid, "Alarm", call_id="abc")
        for i in range(21):
            self.store.set_alarm("2026-11-20T00:00:00Z", str(i), call_id=str(i))
        page = self.store.schedule_status()
        self.assertEqual(20, len(page["pending_alarms"]))
        following = dispatch_schedule(self.store, "read", "schedule_status", {"after_id": page["next_after_id"]})
        self.assertEqual(1, len(following["pending_alarms"]))
        self.assertIsNone(following["next_after_id"])
        with self.assertRaises(ValueError):
            self.store.cancel_alarm(2**67)
        with self.assertRaises(ValueError):
            dispatch_schedule(self.store, "unexpected", "heartbeat", {"minutes": 60, "days": 1})

    def test_budget_holds_work_and_wakes_across_restart_until_exact_utc_reset(self):
        self.now = self.now.replace(hour=23, minute=59)
        self.store.initialize_heartbeat(60)
        self.store.submit("Human message")
        self.assertIsNone(self.store.claim(budget_check=lambda: False))
        cutoff = next_reset(self.now)
        self.assertEqual(cutoff.isoformat(), self.store.schedule_status()["budget_until"])
        store = self.reopen()
        self.now = cutoff - timedelta(microseconds=1)
        self.assertIsNone(store.claim(budget_check=lambda: self.fail("Cap check repeated while held")))
        self.assertEqual(2, len(store.events()))
        self.now = cutoff
        human = store.claim(budget_check=lambda: True)
        self.assertEqual("Human message", human.messages[0].text)
        store.complete(human.id, "Done")
        self.assertIn("Scheduled heartbeat", store.claim(budget_check=lambda: True).messages[0].text)

    def test_mid_turn_hold_queues_continuation_without_repeating_completed_effects(self):
        self.store.initialize_heartbeat(60)
        wake = self.store.claim()
        ran = []

        async def turn(prompt, route, thread, *, scheduled, notify, on_thread, **callbacks):
            self.assertTrue(scheduled)
            on_thread("the-same-thread")
            ran.append(prompt)
            notify("one-receipt", {"text": "A completed effect"})
            raise DailyBudgetHold(dispatched=True, reset_at=next_reset(self.now))

        asyncio.run(process_batch(self.store, wake, turn, "fastest"))
        self.assertEqual(1, len(ran))
        self.assertIsNone(self.store.claim())
        self.now = next_reset(self.now)
        continuation = self.reopen().claim()
        self.assertEqual("the-same-thread", continuation.thread_id)
        self.assertIn("continuation", continuation.messages[0].text)
        self.assertNotIn(ran[0], continuation.messages[0].text)
        self.assertEqual(1, sum(e.text == "A completed effect" for e in self.store.events()))

    def test_first_call_hold_returns_exact_original_ids_without_duplicates(self):
        event_id = self.store.submit("Original human request")
        batch = self.store.claim()

        async def never_dispatched(*args, **kwargs):
            raise DailyBudgetHold(dispatched=False, reset_at=next_reset(self.now))

        asyncio.run(process_batch(self.store, batch, never_dispatched, "cheapest"))
        self.now = next_reset(self.now)
        retry = self.store.claim()
        self.assertEqual([event_id], [event.id for event in retry.messages])

        async def answer(prompt, route, thread, **kwargs):
            self.assertNotIn("scheduled", kwargs)
            return SimpleNamespace(text="Answered"), []

        asyncio.run(process_batch(self.store, retry, answer, "cheapest"))
        self.assertEqual(1, sum(e.text == "Original human request" for e in self.store.events()))
        self.assertIsNone(self.store.claim())
