"""Run one supervised conversation independently of attached terminals."""

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import sys

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
    """Keep each caption beside its image. Codex persists only the private reference."""
    if not any(event.image for event in events):
        return None
    parts = []
    for event in events:
        if event.text:
            parts.append({"type": "text", "text": event.text})
        if event.image:
            url = image_reference(event.id, event.image["sha256"])
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
