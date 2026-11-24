"""Read bounded operational facts from Lantern's existing SQLite ledgers."""

from contextlib import closing
from decimal import Decimal
from pathlib import Path
import re
import sqlite3

from .budget import REQUEST_OUTCOMES, TIMING_FIELDS, UNITS_PER_DOLLAR, dispatch_utc, reported_usd, routing_receipt_sql
from .routing import ROUTES, safe_provider


DIAGNOSTICS_TOOL = {
    "name": "harness_logs",
    "description": "Read recent harness outcomes, tool reviews and model request accounting. "
                   "Use batch_id for one turn and next_before as before for older records. "
                   "Use the recorded event references when reporting an issue.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "batch_id": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
        "before": {"type": "string", "maxLength": 70},
    }, "required": []},
}
FILES = {"chat": "chat.sqlite", "usage": "usage.sqlite", "review": "review-costs.sqlite",
         "supervision": "supervision.sqlite"}
TOOLS = set("""chess_analyze message_operator read_heritage consult_opus workspace_list workspace_read
workspace_write workspace_append workspace_replace heartbeat set_alarm cancel_alarm schedule_status
set_provider_route provider_stats web_search read_web_page budget_status coding_run
github_sync github_preview github_open_draft
harness_logs view_pdf_page unregistered""".split())
STATUSES = set("""complete failed interrupted deferred pending completed incomplete upstream_http_error
invalid_upstream_response settled unconfirmed rejected metered ambiguous archive_error context_unavailable
context_flagged context_clear tool_flagged tool_clear tool_review_unreadable tool_review_unavailable
opus_allowed opus_denied opus_asked_operator opus_advice unregistered_blocked unregistered_clear unregistered_flagged
unregistered_review_unreadable unregistered_review_unavailable unregistered_screen_unavailable
unregistered_opus_allow unregistered_opus_deny context_repeat context_review_unavailable""".split())
OPUS_CODES = set("""invalid_json unexpected_model output_limit incomplete_response invalid_content
response_text_too_long forbidden_xml malformed_xml nested_elements empty_element invalid_verdict
invalid_advice search_limit invalid_search verdict_allow verdict_deny ask_operator advice search invalid_usage
archive_error incomplete_body transport_error response_too_large invalid_concern_ruling
invalid_concern_scope invalid_concern_question indirect_operator_request""".split())
for disposition in ("retire", "watch", "ask_operator"):
    OPUS_CODES.add("concern_" + disposition)
    OPUS_CODES.update("verdict_" + decision + "_concern_" + disposition for decision in ("allow","deny","ask_operator"))
OPUS_CODES.update("verdict_" + decision + "_concerns" for decision in ("allow", "deny", "ask_operator"))
COVERAGE = ("Times use UTC. Model call times are admission times. Qwen route is the requested preference. "
            "From upstream dispatch, response_start_ms measures time to headers, first_token_ms time to the "
            "first observed generation delta (including reasoning or tool arguments), first_output_ms time to visible "
            "text, and response_end_ms time to the terminal response. Provider and reported_cost_usd appear "
            "when observed. Supervision events "
            "without a recorded batch link appear only in the unfiltered view. Luna concern topics "
            "indicate words present in the retained evidence.")


def _positive(value):
    return type(value) is int and 0 < value < 2**63


def _params(args):
    if not isinstance(args, dict) or set(args) - {"batch_id", "limit", "before"}:
        raise ValueError("Use only batch_id, limit and before for harness logs.")
    limit = args.get("limit", 10)
    if not _positive(limit) or limit > 30:
        raise ValueError("Choose a log limit from 1 to 30.")
    if "batch_id" in args and not _positive(args["batch_id"]):
        raise ValueError("Choose a positive batch ID.")
    before = args.get("before")
    if "before" in args:
        match = isinstance(before, str) and len(before) <= 70 and re.fullmatch(
            r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z)\|(chat|qwen|review|screen|flag)\|([1-9][0-9]{0,18})",
            before, re.ASCII)
        if not match or not _positive(int(match[3])):
            raise ValueError("Use the next_before cursor from an earlier logs reply.")
        before = (match[1], match[2], int(match[3]))
    return limit, args.get("batch_id"), before


def _enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _status(value, allowed):
    if isinstance(value, str) and re.fullmatch(r"http_[1-5][0-9]{2}", value):
        return value
    return _enum(value, allowed)


def _amount(value):
    if type(value) is int and value >= 0:
        return format(Decimal(value) / UNITS_PER_DOLLAR, "f")


def _record(kind, row):
    rid = row["rid"]
    source_id = row.get("source_id")
    key = source_id if isinstance(source_id, str) and re.fullmatch(r"[0-9a-f]{32}", source_id) else rid
    event = {"ref": f"{kind}:{key}", "at_utc": row["stamp"], "type": kind}
    for name in ("batch_id", "event_id", "request_sequence", "duration_ms", "input_tokens", "output_tokens",
                 "cache_reads", "cache_writes", "reasoning_tokens", "http_status", *TIMING_FIELDS):
        value = row.get(name)
        if type(value) is int and value >= 0:
            event[name] = value
    for name, allowed in [
        ("tool", TOOLS), ("model", {"luna", "opus", "qwen", "claude-sonnet-4-6"}),
        ("purpose", {"tool", "spot", "consult", "escalation", "context_review", "shadow_review"}),
        ("trigger", {"operator", "heartbeat", "alarm", "continuation", "turn", "worker"}),
        ("charge_source", {"openrouter", "router_no_eligible_endpoint"}), ("route", ROUTES),
        ("request_outcome", REQUEST_OUTCOMES),
        ("flag_kind", {"tool", "context", "unregistered_tool"}),
        ("capture_status", {"pending", "complete", "partial", "archive_error", "no_response"}),
    ]:
        if (value := _enum(row.get(name), allowed)) is not None:
            event[name] = value
    for name, allowed in (("status", STATUSES), ("validation", OPUS_CODES), ("failure_code", OPUS_CODES)):
        if (value := _status(row.get(name), allowed)) is not None:
            event[name] = value
    if row.get("outcome_code"):
        event["outcome_code"] = row["outcome_code"]  # fixed SQL literals only
    if row.get("coding_state"):
        event["coding"] = {"state": row["coding_state"]}
        if type(row.get("code")) is int and -255 <= row["code"] <= 255:
            event["coding"]["exit_code"] = row["code"]
        for key in ("timed_out", "truncated"):
            if row.get(key) in ("true", "false"):
                event["coding"][key] = row[key] == "true"
    if kind == "flag":
        event["evidence_topics"] = [topic for topic in ("credentials", "privileges", "network", "instructions")
                                    if row.get(topic) == 1]
    if kind == "qwen":
        for name, sanitize in (("dispatch_at", dispatch_utc), ("provider", safe_provider),
                               ("reported_cost_usd", reported_usd)):
            if (value := sanitize(row.get(name))) is not None:
                event[name] = value
    if kind in ("review", "qwen"):
        cost, reserved = _amount(row["cost"]), _amount(row["reserved"])
        if cost is not None:
            event["estimated_usd" if kind == "review" else "settled_usd"] = cost
        elif reserved is not None:
            event["held_usd"] = reserved
    return event


def _queries(db, tables, columns):
    if {"chat.batches", "chat.events"} <= tables:
        has_finished_at = any(row["name"] == "finished_at" for row in db.execute("PRAGMA chat.table_info(batches)"))
        completed = "COALESCE(b.finished_at,e.at)" if has_finished_at else "e.at"
        yield "chat", f"""SELECT e.id rid, {completed} AS at, b.id batch_id, e.id event_id, b.status,
          CASE WHEN b.status='failed' AND e.text LIKE '%. Codex reported a provider or turn error'
                    THEN 'codex_provider_or_turn_error'
               WHEN b.status='failed' AND e.text LIKE '%. The provider reached the %,___-token response limit.'
                    THEN 'provider_output_limit'
               WHEN b.status='failed' AND e.text LIKE '%. Codex timed out; do not replay a turn automatically'
                    THEN 'codex_timeout'
               WHEN b.status='failed' AND e.text LIKE '%. Codex turn ended without completing'
                    THEN 'codex_turn_incomplete'
               WHEN b.status='interrupted' THEN 'worker_recovery'
               WHEN b.status='deferred' THEN 'budget_pause' END outcome_code
          FROM chat.batches b JOIN chat.events e ON e.id=b.outcome"""
    if {"usage.requests", "usage.request_details"} <= tables:
        fields, join = routing_receipt_sql(db, tables, "usage.")
        yield "qwen", f"""SELECT d.rowid rid, d.at, d.id source_id, d.batch_id, d.trigger, d.status,
          'qwen' model, d.input_tokens, d.output_tokens, d.cache_reads, d.cache_writes, d.reasoning_tokens,
          d.ms duration_ms, d.charge_source, r.actual cost, r.reserved {fields}
          FROM usage.request_details d JOIN usage.requests r ON r.id=d.id {join}"""
    if "review.calls" in tables:
        batch = "c.batch_id" if "batch_id" in columns else "NULL"
        tool = "c.tool" if "tool" in columns else "NULL"
        audit = "review.opus_responses" in tables
        fields = ",a.http_status,a.capture_status,a.failure_code,a.validation" if audit else ""
        join = " LEFT JOIN review.opus_responses a ON a.request_id=c.id AND c.vendor='opus'" if audit else ""
        yield "review", f"""SELECT c.rowid rid,c.at,c.id source_id,{batch} batch_id,{tool} tool,
          c.vendor model,c.purpose,c.status,c.input_tokens,c.output_tokens,c.cache_reads,c.cache_writes,
          c.ms duration_ms,c.cost,c.reserved {fields} FROM review.calls c {join}"""
    flags = "supervision.luna_flags" in tables
    if flags:
        yield "flag", """SELECT id rid,at,batch_id,sequence request_sequence,tool,kind flag_kind,
            instr(lower(evidence),'credential')>0 credentials, instr(lower(evidence),'privilege')>0 privileges,
            instr(lower(evidence),'network')>0 network, instr(lower(evidence),'instruction')>0 instructions
            FROM supervision.luna_flags"""
    if "supervision.events" in tables:
        links = []
        if flags:
            links.append("SELECT thread,call_id,batch_id FROM supervision.luna_flags")
        if "review.calls" in tables and {"thread_id", "tool_call_id", "batch_id"} <= columns:
            links.append("SELECT thread_id,tool_call_id,batch_id FROM review.calls")
        joins = ""
        batch = "NULL"
        if links:
            joins = f""" LEFT JOIN (SELECT thread,call_id,MIN(batch_id) batch_id FROM (
                {' UNION ALL '.join(links)}) AS links WHERE call_id IS NOT NULL AND batch_id IS NOT NULL
                GROUP BY thread,call_id HAVING COUNT(DISTINCT batch_id)=1) AS link
                ON link.thread=e.thread AND link.call_id=e.call_id"""
            batch = "link.batch_id"
        coding = ""
        if "supervision.actions" in tables:
            joins += """ LEFT JOIN supervision.actions a ON a.thread=e.thread AND a.call_id=e.call_id
                AND (e.event IN ('tool_review_unreadable','tool_review_unavailable','opus_denied')
                OR (e.tool='coding_run' AND e.event IN ('tool_clear','opus_allowed')))"""
            # Only emit named outcomes and typed scalars. The action payload
            # also contains stdout/stderr and must never enter a report.
            safe = "CASE WHEN json_valid(a.result) THEN a.result ELSE '{}' END"
            error = f"json_extract({safe},'$.error')"
            coding = f""",CASE WHEN e.event='opus_denied' THEN 'opus_denied'
                WHEN e.event='tool_review_unreadable' THEN 'opus_response_unreadable'
                WHEN e.event='tool_review_unavailable' THEN
                    CASE WHEN {error} LIKE 'Luna review unavailable.%' THEN 'luna_unavailable'
                         WHEN {error} LIKE 'Opus %unavailable (insufficient daily supervisor allowance).%'
                             THEN 'opus_budget_unavailable'
                         WHEN {error} LIKE 'Opus %unavailable (provider connection failed or was interrupted).%'
                             THEN 'opus_transport_error'
                         WHEN {error} LIKE 'Opus %unavailable (case exceeds the host%review packet limit).%'
                             THEN 'opus_case_too_large'
                         WHEN {error} LIKE 'Opus %unavailable%' THEN 'opus_unavailable' END END outcome_code,
                CASE WHEN e.tool<>'coding_run' OR e.event NOT IN ('tool_clear','opus_allowed')
                          OR a.result IS NULL THEN NULL
                WHEN json_type({safe},'$.error')='text' THEN
                    CASE WHEN json_extract({safe},'$.error') LIKE 'The coding runner is unavailable.%'
                             THEN 'runner_unavailable'
                         WHEN json_extract({safe},'$.error') LIKE 'Coding sandbox unavailable or interrupted.%'
                              OR json_extract({safe},'$.error') LIKE 'The coding runner reply was lost%'
                             THEN 'outcome_uncertain' ELSE 'tool_error' END
                WHEN json_type({safe},'$.timed_out') IN ('true','false') AND
                     json_type({safe},'$.truncated') IN ('true','false') THEN 'returned' END coding_state,
                CASE WHEN json_type({safe},'$.exit_code')='integer' THEN json_extract({safe},'$.exit_code') END code,
                json_type({safe},'$.timed_out') timed_out,json_type({safe},'$.truncated') truncated"""
        yield "screen", f"""SELECT e.rowid rid,e.at,{batch} batch_id,e.tool,e.event status
            {coding} FROM supervision.events e {joins}"""


def read_diagnostics(state: Path, args: dict) -> dict:
    limit, batch, before = _params(args)
    found = []
    unavailable = set()
    # The transient connection supplies SQLite's join engine. Every attached
    # file is an existing, fixed ledger opened mode=ro, including on Windows.
    with closing(sqlite3.connect("file::memory:?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        tables, columns = set(), set()
        for alias, name in FILES.items():
            path = Path(state) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                db.execute(f"ATTACH DATABASE ? AS {alias}", (path.absolute().as_uri() + "?mode=ro",))
                tables.update(f"{alias}.{r[0]}" for r in db.execute(
                    f"SELECT name FROM {alias}.sqlite_master WHERE type='table'"))
            except (sqlite3.Error, OSError):
                unavailable.add(alias)
        if "review.calls" in tables:
            try:
                columns.update(r["name"] for r in db.execute("PRAGMA review.table_info(calls)"))
            except sqlite3.Error:
                unavailable.add("review")
        for kind, query in _queries(db, tables, columns):
            sql = f"""SELECT * FROM (SELECT facts.*, strftime('%Y-%m-%dT%H:%M:%fZ',at) stamp
                     FROM ({query}) AS facts) WHERE stamp IS NOT NULL"""
            params = []
            if batch is not None:
                sql += " AND batch_id=?"
                params.append(batch)
            if before:
                sql += " AND (stamp,?,rid)<(?,?,?)"
                params.extend([kind, *before])
            sql += " ORDER BY stamp DESC,rid DESC LIMIT ?"
            try:
                rows = db.execute(sql, [*params, limit + 1]).fetchall()
            except sqlite3.Error:
                unavailable.add(kind)
                continue
            for row in rows:
                key = (row["stamp"], kind, row["rid"])
                found.append((key, _record(kind, dict(row))))
    found.sort(key=lambda pair: pair[0], reverse=True)
    more = len(found) > limit
    return {"records": [row for _, row in found[:limit]], "has_more": more,
            "next_before": "|".join(map(str, found[limit - 1][0])) if more else None,
            "unavailable_sections": sorted(unavailable), "coverage": COVERAGE}
