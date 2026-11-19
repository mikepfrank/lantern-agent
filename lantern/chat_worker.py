"""Run one supervised conversation independently of attached terminals."""

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import sys

from .codex_session import CodexError, SteeringInput


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


async def process_batch(store, batch, turn, route):
    def take_input():
        events = store.claim_steer(batch.id)
        if events:
            return SteeringInput(tuple(event.id for event in events), "\n\n".join(event.text for event in events))
        return None

    def notify(call_id, arguments):
        if set(arguments) != {"text"} or not isinstance(arguments["text"], str):
            raise ValueError("message_operator requires one text argument")
        text = arguments["text"]
        if not text.strip() or len(text) > 8000:
            raise ValueError("The chat message must contain 1 to 8000 characters")
        return {"event_id": store.notify(batch.id, text, call_id), "delivered_to": "operator chat"}

    prompt = "\n\n".join(event.text for event in batch.messages)
    try:
        result, _ = await turn(
            prompt, route, batch.thread_id, on_thread=lambda tid: store.bind_thread(batch.id, tid),
            take_input=take_input, return_input=lambda ids: store.release_steer(batch.id, ids), notify=notify,
        )
        store.complete(batch.id, result.text)
    except (CodexError, ValueError, OSError, RuntimeError) as exc:
        # No automatic retry: the last tool or paid request might have completed.
        # Do not copy arbitrary third-party exception bodies into the operator chat.
        reason = str(exc) if isinstance(exc, CodexError) else type(exc).__name__
        store.fail(batch.id, reason)
        print(f"Chat batch {batch.id} ended with {type(exc).__name__}", file=sys.stderr, flush=True)


async def serve(state, store, turn, route):
    with worker_lock(state):
        store.recover()
        while True:
            batch = store.claim()
            if batch is None:
                await asyncio.sleep(.35)
            else:
                await process_batch(store, batch, turn, route)
