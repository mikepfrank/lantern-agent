"""Verify the two recorded appeals locally and report only the presence of authority facts.

Run with the candidate project on PYTHONPATH. Opens the metadata databases in
read-only mode. No document access or API calls.
"""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from lantern.operator_review import MAX_STATEMENT
from lantern.review_policy import action_facts
from lantern.supervision import _fingerprint


def _db(state, name):
    path = (state / (name + ".sqlite")).resolve(strict=True)
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _tool_arguments(state, thread, call_ids):
    session, = (state / "codex/sessions").rglob("rollout-*-" + thread + ".jsonl")
    found = {}
    with session.open(encoding="utf-8") as records:
        for line in records:
            if not any(call_id in line for call_id in call_ids):
                continue
            record = json.loads(line)
            item = record["payload"]
            if (record["type"] == "response_item" and item.get("type") == "function_call"
                    and item.get("call_id") in call_ids):
                _require(item["name"] == "read_heritage", "Unexpected historical tool")
                args = json.loads(item["arguments"])
                _require(set(args) == {"filename", "page", "rationale"} and args["filename"] == "lmstudio-chat.pdf"
                         and args["page"] in (12, 13), "Historical operation has changed")
                found[item["call_id"]] = args
    _require(set(found) == call_ids, "Incomplete historical tool call records")
    return found


def _matches_at(db, thread, instant, name):
    # concern_rulings are append-only. Later retirements or new findings cannot
    # change the evidence reported here for the instant before each appeal.
    rows = db.execute("""
        SELECT c.id,r.version,r.status,r.tools FROM context_concerns c
        LEFT JOIN concern_rulings r ON r.concern_id=c.id AND r.version=(
            SELECT MAX(v.version) FROM concern_rulings v WHERE v.concern_id=c.id
            AND julianday(v.at)<=julianday(?))
        WHERE c.thread=? AND julianday(c.created_at)<=julianday(?) ORDER BY c.id""", (instant, thread, instant))
    matches = []
    for number, version, status, scope in rows:
        tools = json.loads(scope) if scope else ["*"]
        if status != "retired" and (name in tools or "*" in tools):
            matches.append({"id": number, "version": version or 0, "status": status or "pending", "tools": tools})
    return matches


def verify_local(state: Path) -> list[dict]:
    with _db(state, "chat") as db:
        thread, = db.execute("SELECT thread_id FROM room WHERE id=1").fetchone()
        receipt = db.execute("""SELECT id,question,answer,answered_at FROM operator_reviews
            WHERE id=4 AND thread=? AND tool='read_heritage' AND status='answered' AND final_decision='allow'""", (thread,)).fetchone()
        _require(receipt and "any pages" in receipt[2], "Missing scoped operator archive ruling")
        archive_receipt = dict(zip(("id", "question", "answer", "answered_at"), receipt, strict=True))
        rows = db.execute("""SELECT e.id,e.at,e.text FROM events e JOIN inbox i ON i.event_id=e.id
            WHERE e.role='operator' AND i.batch_id<=89 ORDER BY e.id DESC LIMIT 3""").fetchall()
        _require(len(rows) == 3 and "retry now" in rows[0][2].lower()
                 and all(len(text) <= MAX_STATEMENT for _, _, text in rows),
                 "Missing or incomplete turn-89 operator statements")
        statements = [dict(zip(("event_id", "at", "text"), r, strict=True)) for r in reversed(rows)]
    with _db(state, "supervision") as db:
        rows = db.execute("""SELECT f.batch_id,f.call_id,f.at,a.digest,a.result
            FROM luna_flags f JOIN actions a ON a.thread=f.thread AND a.call_id=f.call_id
            WHERE f.thread=? AND f.batch_id IN (87,89) AND f.kind='tool' AND f.tool='read_heritage'
            ORDER BY f.batch_id,f.at""", (thread,)).fetchall()
        _require(len(rows) == 4, "Expected the four historical review events")
        arguments = _tool_arguments(state, thread, {row[1] for row in rows})
        prior = {}
        cases = []
        for batch, call_id, instant, digest, result in rows:
            args = arguments[call_id]
            result = json.loads(result)
            _require(_fingerprint(["read_heritage", args]) == digest
                     and result.get("error") == "Claude 3 Opus denied this action.", "Historical receipt mismatch")
            tool_args = {k: v for k, v in args.items() if k != "rationale"}
            page = args["page"]
            if batch == 87:
                prior[page] = (tool_args, result["judgment"])
                continue
            _require(page in prior and prior[page][0] == tool_args, "Original denial covers different arguments")
            matches = _matches_at(db, thread, instant, "read_heritage")
            _require(not matches, "Unexpected applicable watch at admission")
            facts = action_facts("read_heritage", tool_args, operator_statements=statements,
                                 open_concerns=matches, prior_denial=prior[page][1], archive_receipt=archive_receipt)
            grant = facts["standing_grant"]
            checks = {"grant_present": bool(grant and grant.get("operator_receipt") == archive_receipt),
                      "retry_present": facts["operator_statements"][-1] == statements[-1],
                      "no_applicable_watch": facts["open_concerns_for_tool"] == [],
                      "prior_denial_present": bool(prior[page][1]) and facts["prior_exact_denial"] == prior[page][1]}
            _require(all(checks.values()), "One or more historical facts were not bound")
            cases.append({"case": f"page-{page}", **checks})
        _require({c["case"] for c in cases} == {"page-12", "page-13"}, "Incomplete historical pair")
        return cases


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path, help="Private service state directory on the operator host.")
    args = parser.parse_args()
    print(json.dumps(verify_local(args.state)))
