"""Incrementally read the bound room's original, model-visible native tool records."""

from contextlib import contextmanager
import hashlib
import json
import os

from .thread_upgrade import _room_rollout


# Tool IPC is capped at 4 MiB. Allow for JSON escaping in the saved rollout.
MAX_LINE = 16 * 1024 * 1024
_REVIEW_FIELDS = {"opus_review_text", "opus_advice_text", "opus_context_review"}
_ERROR_FIELDS = {"error", "error_code", "judgment", "review_id", "question"}

# These tools operate on public sources, receipts or model-visible measurements.
# Revisit this list when adding a tool; an unknown tool only gets a reference.
_ORDINARY = frozenset("""
    chess_analyze budget_status provider_stats set_provider_route web_search read_web_page
    import_file clone_repository file_import_status repository_clone_status github_sync
    github_open_draft request_file_import request_repository_clone heartbeat schedule_status
    cancel_alarm workspace_list
""".split())
_WORKSPACE_CALL = {"path", "offset", "length", "expected_sha256"}
_WORKSPACE_RESULT = {"path", "size_bytes", "next_offset", "sha256", "sha256_prefix"}
_SELECTED = {
    "workspace_read": (_WORKSPACE_CALL, _WORKSPACE_RESULT),
    "workspace_write": (_WORKSPACE_CALL, _WORKSPACE_RESULT),
    "workspace_append": (_WORKSPACE_CALL, _WORKSPACE_RESULT),
    "workspace_replace": (_WORKSPACE_CALL, _WORKSPACE_RESULT),
    "coding_run": (set(), {"exit_code", "timed_out", "truncated"}),
    "github_preview": ({"commit"}, {"base", "base_branch", "paths", "proposal_id", "repository", "source_commit"}),
    "message_operator": (set(), {"delivered_to", "event_id"}),
    "set_alarm": ({"at_utc"}, {"at_utc", "alarm_id", "status"}),
    "view_pdf_page": ({"path", "page"}, {"path", "page", "page_count", "width", "height"}),
    "read_heritage": ({"filename", "page", "offset", "length"},
                      {"filename", "path", "page", "offset", "next_offset", "size_bytes"}),
    "harness_logs": ({"batch_id", "before", "limit"}, set()),
    "consult_opus": (set(), {"advice"}),
}
_OMITTED = "[Content omitted from history lookup.]"


def _remove_review_fields(value):
    """Remove supervisor side channels even when a receipt wraps another receipt."""
    changed = False
    pending = [value]
    while pending:
        obj = pending.pop()
        if isinstance(obj, dict):
            for key in _REVIEW_FIELDS.intersection(obj):
                del obj[key]
                changed = True
            pending.extend(v for v in obj.values() if isinstance(v, (dict, list)))
        elif isinstance(obj, list):
            pending.extend(v for v in obj if isinstance(v, (dict, list)))
    return changed


def _body(name, source, value):
    """Preserve the exact JSON string for eligible, unfiltered native payloads."""
    if name not in _ORDINARY and name not in _SELECTED:
        return _OMITTED
    omitted = False
    if isinstance(value, list):
        # Multimodal results have text and a separate private image reference.
        # Never copy image bytes, references, or unexpected content parts.
        parts = [p.get("text") for p in value if isinstance(p, dict)
                 and p.get("type") in ("input_text", "output_text") and isinstance(p.get("text"), str)]
        if len(parts) != 1:
            return _OMITTED
        omitted = len(value) != 1
        value = parts[0]
    if not isinstance(value, str):
        return _OMITTED
    try:
        obj = json.loads(value)
    except (ValueError, RecursionError):
        return _OMITTED
    if not isinstance(obj, dict):
        return _OMITTED
    if source == "result" and _remove_review_fields(obj):
        omitted = True
    if name in _SELECTED:
        call_fields, result_fields = _SELECTED[name]
        keep = call_fields if source == "call" else result_fields | _ERROR_FIELDS
        selected = {k: v for k, v in obj.items() if k in keep}
    else:
        selected = obj
    if len(selected) == len(obj) and not omitted:
        return value
    if not selected:
        return _OMITTED
    return json.dumps(selected, ensure_ascii=False) + "\n" + _OMITTED


def _event(record, lookup_tool, native_turn):
    if record.get("type") != "response_item":
        return None
    item = record.get("payload")
    if not isinstance(item, dict):
        return None
    if item.get("type") == "message" and item.get("role") == "assistant":
        item_id = item.get("id")
        if native_turn and isinstance(item_id, str) and item_id:
            return {"source": "anchor", "item_id": item_id, "native_turn": native_turn}
    if item.get("type") not in ("function_call", "function_call_output"):
        return None
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id or len(call_id) > 512 or not call_id.isprintable():
        raise ValueError("Native history contains a tool record without a usable call ID.")
    is_call = item["type"] == "function_call"
    name = item.get("name") if is_call else lookup_tool(call_id)
    if name in ("history_search", "history_open") or (not is_call and name is None):
        # No result can be exposed without a matching indexed call. The lookup
        # tools themselves produce copies of history, not fresh evidence.
        return None
    if not isinstance(name, str) or not name:
        raise ValueError("Native history contains a tool call without a tool name.")
    at = record.get("timestamp")
    if not isinstance(at, str) or not at:
        raise ValueError("Native history contains a tool record without a timestamp.")
    source = "call" if is_call else "result"
    return {"ref": f"{source}:{call_id}", "source": source, "call_id": call_id,
            "tool": name, "at": at, "native_turn": native_turn,
            "text": _body(name, source, item.get("arguments" if is_call else "output"))}


def _record(line):
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        raise ValueError("Unreadable native history record.") from None
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise ValueError("Malformed native history record.")
    if (record["type"] in ("session_meta", "turn_context", "event_msg", "response_item")
            and not isinstance(record.get("payload"), dict)):
        raise ValueError("Malformed native history record.")
    return record


class _CallStream:
    def __init__(self, file, thread_id):
        self.file = file
        self.native_turn = None
        stat = os.fstat(file.fileno())
        self.size = stat.st_size
        header = file.readline(MAX_LINE + 1)
        if len(header) > MAX_LINE or not header.endswith(b"\n"):
            raise ValueError("Incomplete or oversized native history header.")
        record = _record(header)
        if record.get("type") != "session_meta" or record["payload"].get("id") != thread_id:
            raise ValueError("Native history belongs to a different thread.")
        self.identity = f"{stat.st_dev}:{stat.st_ino}:{hashlib.sha256(header).hexdigest()}"

    def read(self, start_offset, lookup_tool, native_turn=None):
        """Yield the committed byte offset and optionally one eligible event.

        lookup_tool reads the catalogue by native call ID, including rows added
        in this transaction. A result may arrive in a later refresh or process.
        Preserve native_turn together with the offset across refreshes; assistant
        item IDs are anchor markers for exact joins to the room's reply blocks.
        The snapshot ends before an incomplete final record.
        """
        if not 0 <= start_offset <= self.size:
            raise ValueError("Native history offset is outside the saved rollout.")
        file = self.file
        self.native_turn = native_turn
        file.seek(start_offset)
        while file.tell() < self.size:
            line = file.readline(min(MAX_LINE + 1, self.size - file.tell()))
            if len(line) > MAX_LINE:
                raise ValueError("Native history contains an oversized record.")
            if not line.endswith(b"\n"):
                break
            record = _record(line)
            payload = record.get("payload")
            kind = record.get("type")
            if kind == "turn_context" or (kind == "event_msg" and payload.get("type") == "task_started"):
                value = payload.get("turn_id")
                self.native_turn = value if isinstance(value, str) and value else None
            elif kind == "event_msg" and payload.get("type") in ("task_complete", "turn_aborted"):
                if payload.get("turn_id") == self.native_turn:
                    self.native_turn = None
            yield file.tell(), _event(record, lookup_tool, self.native_turn)


@contextmanager
def call_event_stream(codex_home, thread_id):
    """Open the registered room rollout read-only, excluding backups and other threads."""
    path = _room_rollout(codex_home, thread_id)
    with path.open("rb") as file:
        yield _CallStream(file, thread_id)
