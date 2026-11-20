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

from .codex_session import BASE_INSTRUCTIONS
from .coding_tools import CODING_TOOL
from .diagnostics import DIAGNOSTICS_TOOL
from .pdf_tool import PDF_TOOL
from .supervision import supervised_tools


def _update_room_meta(meta, tools, *, independent):
    changed = _add_specifications(meta["dynamic_tools"], ({"type": "function", **tool} for tool in tools))
    if independent:
        # Exact base shipped through the private heritage release. Keep custom
        # bases and the separate developer persona intact.
        base = meta.get("base_instructions") or {}
        text = base.get("text", "") if isinstance(base, dict) else ""
        if hashlib.sha256(text.encode()).hexdigest() == "353d13ba9bdc18af95f5077ed57e0d2183647b867a2434a0511bac068903ded7":
            base["text"] = BASE_INSTRUCTIONS
            changed = True
    return changed


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


def _add_specifications(saved, specifications):
    changed = False
    for spec in specifications:
        existing = [entry for entry in saved if entry.get("name") == spec["name"]]
        if existing and existing != [spec]:
            raise ValueError("Saved tool has a conflicting definition")
        if not existing:
            saved.append(spec)
            changed = True
    return changed


def _update_checkpoint(payload, thread_id, specifications):
    # A compacted rollout can carry a registry scoped to a specific thread.
    # Absent and foreign registries are not overrides for this room.
    checkpoint = (payload.get("resume_metadata") or {}).get("dynamic_tools")
    if checkpoint is None or checkpoint.get("thread_id") != thread_id:
        return False
    return _add_specifications(checkpoint["tools"], specifications)


def _rewrite(backup, out, thread_id, tools, old_persona, new_persona, *, checkpoints=False):
    # Codex persists the first session_meta tool list and stores developer text
    # in rollout messages. Its resume RPC cannot replace either in an old thread.
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
                if checkpoints:
                    specifications = payload["dynamic_tools"]
                else:
                    changed = _update_room_meta(payload, tools, independent=old_persona is None)
            if record["type"] == "response_item":
                items = [payload]
            elif record["type"] == "compacted":
                if checkpoints:
                    changed = _update_checkpoint(payload, thread_id, specifications)
                items = payload.get("replacement_history") or []
            else:
                items = []
            for item in items:
                if item.get("type") == "message" and item.get("role") == "developer":
                    for part in item.get("content", []):
                        if old_persona is not None and part.get("type") == "input_text" and part.get("text") == old_persona:
                            part["text"] = new_persona
                            changed = True
                            matches += 1
            data = ((json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                    if changed else raw)
            out.write(data)
            old, new = offsets[-1]
            offsets.append((old + len(raw), new + len(data)))
    if old_persona is not None and not matches:
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
    return _migrate(codex_home, thread_id, [tool], backup_dir, "heritage", old_persona, new_persona)


def install_room_tools(state, thread_id, tools):
    """Called under the sole worker lock before accepting input or generating wakeups."""
    if thread_id is None:
        return
    if str(UUID(thread_id)) != thread_id:
        raise ValueError("Invalid private room thread ID")
    home = state / "codex"
    backups = state / "operator-backups"
    rollout = _room_rollout(home, thread_id)
    checkpoint_dir = backups / "compaction-tools-v1"
    # This step can have completed the rename but not the index commit. Repair
    # from its own source snapshot before attempting a newly configured upgrade.
    for backup in sorted(checkpoint_dir.glob(f"{thread_id}.before-*.jsonl")):
        if not backup.with_suffix(".done").exists():
            _migrate(home, thread_id, [], checkpoint_dir, backup.stem.split(".before-", 1)[1],
                     checkpoints=True)
    optional = {"coding-workspace": CODING_TOOL, "harness-logs": DIAGNOSTICS_TOOL, "pdf-pages": PDF_TOOL}
    additions = {tool["name"]: label for label, tool in optional.items()}
    stages = {label: [] for label in ("independent-work", *optional)}
    for tool in tools:
        stages[additions.get(tool["name"], "independent-work")].append(tool)

    # Each stage has an immutable backup of its own starting point. Recover a
    # partially replaced rollout/index BEFORE installing any newly enabled tool.
    report = None
    for label, specifications in stages.items():
        directory = backups / f"{label}-v1"
        backup = directory / f"{thread_id}.before-{label}.jsonl"
        if not backup.exists() or backup.with_suffix(".done").exists():
            continue
        recovery_specs = specifications
        if not specifications and label in optional:
            recovery_specs = [supervised_tools([optional[label]])[0]]
        report = _migrate(home, thread_id, recovery_specs, directory, label)

    for label, specifications in stages.items():
        if specifications:
            report = _migrate(home, thread_id, specifications, backups / f"{label}-v1", label) or report
    # 0.169 prefers the saved compaction checkpoint over the opening registry
    # on a later resume. A completed earlier rollout migration is still valid:
    # start any checkpoint repair from the complete current history.
    with rollout.open("rb") as stream:
        first = json.loads(stream.readline())
        if first["type"] != "session_meta" or first["payload"].get("id") != thread_id:
            raise ValueError("Saved thread identity mismatch")
        canonical = first["payload"]["dynamic_tools"]
        for raw in stream:
            if b'"compacted"' not in raw:
                continue
            record = json.loads(raw)
            if record["type"] == "compacted" and _update_checkpoint(record["payload"], thread_id, canonical):
                break
        else:
            return report
    # A room may gain more tools after many subsequent turns. A source digest
    # distinguishes each revision's private backup and crash recovery metadata.
    label = "checkpoint-" + _digest(rollout)
    return _migrate(home, thread_id, [], checkpoint_dir, label, checkpoints=True) or report


def _room_rollout(codex_home, thread_id):
    home = codex_home.resolve(strict=True)
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as registry:
        row = registry.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not row:
        raise ValueError("Native thread is missing")
    rollout = Path(row[0]).resolve(strict=True)
    sessions = (home / "sessions").resolve(strict=True)
    # After the first resume, Windows Codex can persist the same absolute path
    # in extended-length form. Verify directory identity for either spelling.
    if not rollout.name.endswith(thread_id + ".jsonl") or not any(p.samefile(sessions) for p in rollout.parents):
        raise ValueError("Unexpected native rollout path")
    return rollout


def _migrate(codex_home, thread_id, tools, backup_dir, label, old_persona=None, new_persona=None, *, checkpoints=False):
    home = codex_home.resolve(strict=True)
    rollout = _room_rollout(home, thread_id)
    backup = backup_dir / f"{thread_id}.before-{label}.jsonl"
    done = backup.with_suffix(".done")
    if not checkpoints and old_persona is None and (done.exists() or not backup.exists()):
        with rollout.open("rb") as stream:
            meta = json.loads(stream.readline())
        if meta["type"] != "session_meta" or meta["payload"]["id"] != thread_id:
            raise ValueError("Saved thread identity mismatch")
        if not _update_room_meta(meta["payload"], tools, independent=True):
            return
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _backup(rollout, backup)
    old_digest = _digest(backup)
    with NamedTemporaryFile(dir=rollout.parent, prefix=".heritage-", delete=False) as out:
        staged = Path(out.name)
        try:
            offsets = _rewrite(backup, out, thread_id, tools, old_persona, new_persona, checkpoints=checkpoints)
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
            if old_persona is None:
                done.touch(mode=0o600)
            return {"previous_sha256": old_digest, "updated_sha256": new_digest, "records": len(offsets) - 1}
        finally:
            out.close()
            staged.unlink(missing_ok=True)
