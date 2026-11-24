"""Run one supervised conversation independently of attached terminals."""

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import sys
from threading import Lock

from .codex_session import CodexError, SteeringInput
from .image_ref import image_reference
from .scheduler import DailyBudgetHold


MESSAGE_TOOL = {
    "name": "message_operator",
    "description": "Deliver a deliberate interim message to the operator's persistent chat.",
    "inputSchema": {"type": "object", "additionalProperties": False,
                    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 8000}},
                    "required": ["text"]},
}


class WorkerBusy(RuntimeError):
    pass


class RoomReplyDelivery:
    """Match native visible messages to settled provider responses for one room turn."""

    def __init__(self, publish):
        self.publish = publish
        self.lock = Lock()
        self.pending = {}
        self.confirmed = set()
        self.saw_message = False
        self.closed = False
        self.error = None

    def receive(self, item_id, text):
        with self.lock:
            self.saw_message = True
            self.pending[item_id] = text
            self._flush()
            self._check_error()

    def confirm(self, item_ids):
        # Called on the gateway's HTTP thread. Either half of the pair may arrive first.
        with self.lock:
            if not self.closed:
                self.confirmed.update(item_ids)
                self._flush()

    def _flush(self):
        while self.pending and self.error is None:
            item_id = next(iter(self.pending))
            if item_id not in self.confirmed:
                break
            try:
                self.publish(item_id, self.pending[item_id])
            except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
                # Surface storage errors on the worker without disturbing provider settlement.
                self.error = exc
                break
            del self.pending[item_id]
            self.confirmed.remove(item_id)

    def _check_error(self):
        if self.error is not None:
            raise CodexError("Unable to save a completed reply block in the room") from self.error

    def finish(self, *, all_sources_complete):
        # Called only after the gateway's existing final-turn validation. Some
        # providers omit stable item IDs. On a clean turn their native text is
        # safe to release here. On a steered turn require positive attribution.
        with self.lock:
            if all_sources_complete:
                self.confirmed.update(self.pending)
            else:
                self.pending = {key: text for key, text in self.pending.items() if key in self.confirmed}
            self._flush()
            self._check_error()
            return self.saw_message

    def close(self):
        with self.lock:
            self.closed = True


@contextmanager
def worker_lock(state: Path):
    # Kernel releases this lock even if the owner dies. Keep the inode on disk
    # so concurrent starts all lock the same file.
    with (state / "chat-worker.lock").open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkerBusy("A Lantern chat worker is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkerBusy("A Lantern chat worker is already running") from exc
        yield


def worker_online(state: Path) -> bool:
    try:
        with worker_lock(state):
            return False
    except WorkerBusy:
        return True


def _message_parts(events):
    """Keep each caption with its ordered images. Codex saves only private references."""
    if not any(event.images for event in events):
        return None
    parts = []
    for event in events:
        if event.text:
            parts.append({"type": "text", "text": event.text})
        for image in event.images:
            url = image_reference(event.id, image["sha256"])
            parts.append({"type": "image", "url": url})
    return parts


async def process_batch(store, batch, turn, route):
    def take_input():
        events = store.claim_steer(batch.id)
        if events:
            return SteeringInput(tuple(event.id for event in events), "\n\n".join(event.text for event in events),
                                 _message_parts(events))
        return None

    def notify(call_id, arguments):
        if set(arguments) != {"text"} or not isinstance(arguments["text"], str):
            raise ValueError("message_operator requires one text argument")
        text = arguments["text"]
        if not text.strip() or len(text) > 8000:
            raise ValueError("The chat message must contain 1 to 8000 characters")
        return {"event_id": store.notify(batch.id, text, call_id), "delivered_to": "operator chat"}

    prompt = "\n\n".join(event.text for event in batch.messages)
    source = {"scheduled": True} if batch.messages[0].role == "system" else {}
    if parts := _message_parts(batch.messages):
        source["input_parts"] = parts
    trigger = "operator"
    if source.get("scheduled"):
        first = batch.messages[0].text
        if first.startswith("Scheduled heartbeat."):
            trigger = "heartbeat"
        elif first.startswith("Scheduled alarm #"):
            trigger = "alarm"
        else:
            trigger = "continuation"
    try:
        result, _ = await turn(
            prompt, route, batch.thread_id, on_thread=lambda tid: store.bind_thread(batch.id, tid),
            take_input=take_input, return_input=lambda ids: store.release_steer(batch.id, ids), notify=notify,
            on_message=lambda item_id, text: store.append_reply_block(batch.id, item_id, text),
            batch_id=batch.id, trigger=trigger, **source,
        )
        store.complete(batch.id, result.text)
    except DailyBudgetHold as hold:
        store.defer_for_budget(batch.id, hold)
    except (CodexError, ValueError, OSError, RuntimeError) as exc:
        # No automatic retry: the last tool or paid request might have completed.
        # Do not copy arbitrary third-party exception bodies into the operator chat.
        reason = str(exc) if isinstance(exc, CodexError) else type(exc).__name__
        store.fail(batch.id, reason)
        print(f"Chat batch {batch.id} ended with {type(exc).__name__}", file=sys.stderr, flush=True)


async def serve(state, store, turn, route, *, heartbeat_minutes=None, budget_check=None, on_start=None):
    with worker_lock(state):
        store.recover()
        if on_start is not None:
            on_start()
        if heartbeat_minutes is not None:
            store.initialize_heartbeat(heartbeat_minutes)
        while True:
            batch = store.claim(budget_check=budget_check, include_scheduled=heartbeat_minutes is not None)
            if batch is None:
                await asyncio.sleep(.35)
            else:
                await process_batch(store, batch, turn, route)
