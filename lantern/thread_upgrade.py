"""Offline operator migration for the existing private Codex 0.169 thread."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
from shutil import copyfileobj
import sqlite3
from tempfile import NamedTemporaryFile
from uuid import UUID


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _backup(source, backup):
    if backup.exists():
        return
    with NamedTemporaryFile(dir=backup.parent, delete=False) as dst:
        temp = Path(dst.name)
        try:
            with source.open("rb") as stream:
                copyfileobj(stream, dst)
            dst.flush()
            os.fsync(dst.fileno())
            dst.close()
            if _digest(temp) != _digest(source):
                raise ValueError("Saved thread changed during backup")
            os.replace(temp, backup)
        finally:
            dst.close()
            temp.unlink(missing_ok=True)


def _rewrite(backup, out, thread_id, tool, old_persona, new_persona):
    # Codex persists the first session_meta tool list and stores developer text
    # in rollout messages. Its resume RPC cannot replace either in an old thread.
    spec = {"type": "function", **tool}
    matches = 0
    offsets = [(0, 0)]
    with backup.open("rb") as src:
        for i, raw in enumerate(src):
            record = json.loads(raw)
            if record.get("ordinal", i) != i:
                raise ValueError("Unexpected saved-thread ordinal")
            payload = record["payload"]
            changed = False
            if i == 0:
                if record["type"] != "session_meta" or payload.get("id") != thread_id:
                    raise ValueError("Saved thread identity mismatch")
                tools = payload["dynamic_tools"]
                existing = [t for t in tools if t.get("name") == spec["name"]]
                if existing and existing != [spec]:
                    raise ValueError("Saved tool has a conflicting definition")
                if not existing:
                    tools.append(spec)
                    changed = True
            if record["type"] == "response_item":
                items = [payload]
            elif record["type"] == "compacted":
                items = payload.get("replacement_history") or []
            else:
                items = []
            for item in items:
                if item.get("type") == "message" and item.get("role") == "developer":
                    for part in item.get("content", []):
                        if part.get("type") == "input_text" and part.get("text") == old_persona:
                            part["text"] = new_persona
                            changed = True
                            matches += 1
            data = ((json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                    if changed else raw)
            out.write(data)
            old, new = offsets[-1]
            offsets.append((old + len(raw), new + len(data)))
    if not matches:
        raise ValueError("Original developer persona does not match the saved thread")
    return offsets


def _translate(offsets, ordinal, offset):
    if offset is None:
        return None
    if not 0 <= ordinal < len(offsets) or offset not in offsets[ordinal]:
        raise ValueError("Unrecognized native thread-history index offset")
    return offsets[ordinal][1]


def _reindex(db, thread_id, offsets):
    for turn, start, end, pos, last in db.execute("""
            SELECT turn_id,rollout_ordinal,rollout_end_ordinal,rollout_byte_offset,rollout_end_byte_offset
            FROM thread_turns WHERE thread_id=?""", (thread_id,)).fetchall():
        db.execute("""UPDATE thread_turns SET rollout_byte_offset=?,rollout_end_byte_offset=?
                      WHERE thread_id=? AND turn_id=?""",
                   (_translate(offsets, start, pos), _translate(offsets, end + 1 if end is not None else -1, last),
                    thread_id, turn))
    state = db.execute("""SELECT next_rollout_ordinal,next_rollout_byte_offset
                          FROM thread_history_projection_state WHERE thread_id=?""", (thread_id,)).fetchone()
    if state is not None:
        db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?",
                   (_translate(offsets, *state), thread_id))


def upgrade_saved_thread(codex_home: Path, thread_id: str, *, old_persona: str, new_persona: str,
                         tool: dict, backup_dir: Path) -> dict:
    """Call with the worker stopped. Retry an interrupted migration with the same private backup."""
    if str(UUID(thread_id)) != thread_id or not old_persona or not new_persona or tool["name"] != "read_heritage":
        raise ValueError("Invalid heritage migration parameters")
    home = codex_home.resolve(strict=True)
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as registry:
        row = registry.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not row:
        raise ValueError("Native thread is missing")
    rollout = Path(row[0]).resolve(strict=True)
    if not rollout.is_relative_to((home / "sessions").resolve(strict=True)) or not rollout.name.endswith(thread_id + ".jsonl"):
        raise ValueError("Unexpected native rollout path")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = backup_dir / (thread_id + ".before-heritage.jsonl")
    _backup(rollout, backup)
    old_digest = _digest(backup)
    with NamedTemporaryFile(dir=rollout.parent, prefix=".heritage-", delete=False) as out:
        staged = Path(out.name)
        try:
            offsets = _rewrite(backup, out, thread_id, tool, old_persona, new_persona)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            new_digest = _digest(staged)
            current = _digest(rollout)
            if current not in (old_digest, new_digest):
                raise ValueError("Saved conversation changed since the backup")
            history_db = (home / "thread_history_1.sqlite").as_uri() + "?mode=rw"
            with closing(sqlite3.connect(history_db, uri=True)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                _reindex(db, thread_id, offsets)
                if current != new_digest:
                    os.replace(staged, rollout)
            return {"previous_sha256": old_digest, "updated_sha256": new_digest, "records": len(offsets) - 1}
        finally:
            out.close()
            staged.unlink(missing_ok=True)
