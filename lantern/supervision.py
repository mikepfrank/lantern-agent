"""Review Lantern's proposed actions before dispatching any tool."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Condition, local
from typing import NamedTuple

from . import concerns
from .github_tools import OPEN_DRAFT
from .opus import OPERATOR_QUESTION_LIMIT, OpusCaseTooLarge, OpusResponseError
from .review_policy import action_facts, concern_refs, ruling_query
from .review_vendors import HTTPRejection, OpusTransportError, ReviewBudgetUnavailable
from .workspace import workspace_edit_evidence


CONSULT = "consult_opus"
TOOLS = {"chess_analyze", "message_operator", "read_heritage", CONSULT}
RATIONALE_LIMIT = 1200
DATABASE = "supervision.sqlite"


class ReviewUnavailable(RuntimeError):
    """Required review could not complete. A request must remain unexecuted."""


class ReviewUnreadable(ReviewUnavailable):
    """The reviewer answered but the response did not pass the host's validation."""


def _opus_failure(error, *, batch_id, consultation=False):
    purpose = "consultation" if consultation else "review"
    if isinstance(error, OpusResponseError):
        turn = f" for turn {batch_id}" if batch_id is not None else ""
        if error.code == "indirect_operator_request":
            return ReviewUnreadable("The reviewer requested operator input outside the direct question channel. "
                                    "The review has no valid ruling. Leave the action held and do not seek "
                                    f"authorization through chat. The host archived the reply{turn}.")
        status = ("No advice was delivered." if consultation else
                  "The proposed action did not run and remains held. Leave it pending.")
        return ReviewUnreadable(f"Opus replied, but its {purpose} response was unreadable ({error.code}). "
                                f"{status} Report this to Mike so he can inspect the archived reply{turn}.")
    if isinstance(error, HTTPRejection):
        return ReviewUnavailable(f"Opus {purpose} unavailable (provider returned HTTP {error.status})")
    if isinstance(error, OpusTransportError):
        return ReviewUnavailable(f"Opus {purpose} unavailable (provider connection failed or was interrupted)")
    if isinstance(error, OpusCaseTooLarge):
        return ReviewUnavailable(f"Opus {purpose} unavailable (case exceeds the host's review packet limit)")
    if isinstance(error, ReviewBudgetUnavailable):
        return ReviewUnavailable(f"Opus {purpose} unavailable (insufficient daily supervisor allowance)")
    return ReviewUnavailable(f"Opus {purpose} unavailable")


def _save_reply_text(feedback, field, text):
    if feedback is not None and isinstance(text, str) and text:
        feedback[field] = text


class ReviewedContext(NamedTuple):
    sequence: int
    recent: str


def supervised_tools(tools: list[dict]) -> list[dict]:
    tools = deepcopy(tools)
    tools.append({
        "name": CONSULT,
        "description": "Ask Claude 3 Opus for ethical advice about a specific situation. Advice cannot authorize a subsequent tool call.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {"question": {"type": "string", "maxLength": 2400}},
            "required": ["question"],
        },
    })
    for tool in tools:
        schema = tool["inputSchema"]
        schema["properties"]["rationale"] = {
            "type": "string", "minLength": 1, "maxLength": RATIONALE_LIMIT,
            "description": "Brief public justification for this exact action and its intended effect.",
        }
        schema.setdefault("required", []).append("rationale")
    return tools


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def review_summary(state: Path) -> dict:
    """All-time event counts for calibrating spot checks, referrals and outcomes."""
    path = state / DATABASE
    if not path.exists():
        return {}
    with database(path) as db:
        return dict(db.execute("SELECT event, COUNT(*) FROM events GROUP BY event"))


def visible_context(payload: dict) -> dict:
    context = {key: payload[key] for key in ("instructions", "input", "tools") if key in payload}
    if isinstance(context.get("input"), list):
        # Provider-private reasoning items and their encrypted state are not conversation text.
        context["input"] = [item for item in context["input"] if item.get("type") != "reasoning"]
    return context


def recent_messages(context: dict) -> str:
    items = context.get("input", [])
    if isinstance(items, str):
        return items[-2400:]
    web_reads = {item["call_id"] for item in items if item.get("type") == "function_call"
                 and item.get("name") == "read_web_page" and isinstance(item.get("call_id"), str)}
    excerpts = []
    for item in reversed(items):
        if item.get("type") == "function_call_output":
            role, content = "tool result", item.get("output", "")
            if item.get("call_id") in web_reads:
                role = "untrusted external webpage tool result"
        elif item.get("role") in ("assistant", "user"):
            role, content = item["role"], item.get("content", "")
        else:
            continue
        if isinstance(content, list):
            image_label = "[Rendered PDF page from tool]" if role == "tool result" else "[Operator image attached]"
            content = "\n".join(image_label if p.get("type") == "input_image" else p["text"]
                                for p in content if p.get("type") == "input_image" or "text" in p)
        excerpts.append(f"{role}: {content[-1200:]}")
        if len(excerpts) == 4:
            break
    return "\n".join(reversed(excerpts))


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def question_scope(name, args):
    # Reading another page or revising a draft can raise the same question about
    # intent. The actual tool arguments are always reviewed before dispatch.
    path = args.get("path")
    if name in ("workspace_read", "workspace_write") and isinstance(path, str) and 0 < len(path) <= 512:
        return f"{name}:{path}"
    return f"{name}:{_fingerprint(args)}"


def describe_action(name, args):
    if name in ("workspace_read", "workspace_write") and isinstance(args.get("path"), str):
        path = args["path"][:512]
        if name == "workspace_write" and isinstance(args.get("text"), str):
            return f"Write {len(args['text']):,} characters to {path}"
        if name == "workspace_read":
            return f"Read {path} from offset {args.get('offset', 0)}"
    text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return f"{name} {text[:900]}" + ("…" if len(text) > 900 else "")


def _attestation(row):
    return {key: row[key] for key in ("id", "source", "call_id", "asked_at", "action", "excerpt",
                                      "question", "answered_at", "answer")}


def _review_excerpt(case):
    evidence = [case["evidence"], case.get("concern_review", {}).get("evidence", "")]
    for item in case.get("concern_reviews", []):
        evidence.append(f"#{item['id']}: {item['evidence']}")
    concern = "\n".join(filter(None, evidence)) or case["summary"]
    excerpt = "Review concern\n" + (concern if len(concern) <= 950 else concern[:949] + "…")
    edit = case.get("host_facts", {}).get("workspace_edit", {})
    if edit.get("hunks"):
        changed = json.dumps(edit["hunks"], ensure_ascii=False)
        excerpt += "\n\nChanged text (offsets in characters)"
        if not edit["complete"]:
            excerpt += "\nSome changes were omitted from this preview."
        if len(changed) > 700:
            excerpt += "\nThe displayed text is truncated."
            changed = changed[:699] + "…"
        excerpt += "\n" + changed
    elif edit.get("status") == "unchanged":
        excerpt += "\n\nThe proposed file is identical to the verified revision."
    elif case["tool"] == "workspace_write" and isinstance(case["arguments"].get("text"), str):
        draft = case["arguments"]["text"]
        if edit.get("status") == "replace":
            excerpt += "\n\nThe changes exceed the preview limit."
        elif edit.get("status") != "create":
            excerpt += "\n\nThe host could not verify the saved revision."
        excerpt += "\n\nProposed text" + (" (ending)\n…" + draft[-700:] if len(draft) > 700 else "\n" + draft)
    if case["tool"] == OPEN_DRAFT and (draft := case.get("host_facts", {}).get("public_draft")):
        text = f"{draft['repository']} → {draft['base_branch']}\n{draft['title']}\n" + "\n".join(draft["paths"])
        excerpt += "\n\nPublic draft pull request\n" + text[:900]
    return excerpt


def recovered_excerpt(error, evidence_excerpt):
    prefix = ("Opus requested an operator decision through Lantern. The host redirected it here."
              if error.code == "indirect_operator_request" else
              f"Opus asked for clarification in a ruling the host could not validate ({error.code}).")
    if error.action_question and error.concern_question:
        prefix += "\n\nRelated context question from the same reply\n" + error.concern_question
    return (prefix + "\n\n" + evidence_excerpt)[:2000]


class Supervisor:
    def __init__(self, state: Path, luna, review, consult, *, sample_every: int = 12, tool_names=None,
                 batch_id: int | None = None, operator_room=None, draft_evidence=None, workspace_root=None):
        if type(sample_every) is not int or sample_every < 1:
            raise ValueError("Sample interval must be a positive integer")
        self.path = state / DATABASE
        self.luna, self.review, self.consult = luna, review, consult
        self.sample_every = sample_every
        self.tool_names = frozenset(TOOLS if tool_names is None else tool_names)
        self.batch_id = batch_id
        self.operator_room = operator_room
        self.draft_evidence = draft_evidence
        self.workspace_root = workspace_root
        self.thread = None
        self._observations = Condition()
        self._received = self._processed = 0
        self._current = None
        self._origin = local()
        with database(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS threads(
                    thread TEXT PRIMARY KEY, requests INTEGER NOT NULL, sampled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS context_scan_cursors(
                    thread TEXT PRIMARY KEY, last_event INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS pending_context_replies(
                    thread TEXT NOT NULL, concern_id INTEGER NOT NULL, batch_id INTEGER,
                    parsed_decision TEXT, response_text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS findings(
                    thread TEXT, sequence INTEGER, evidence TEXT NOT NULL, summary TEXT NOT NULL,
                    heard INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(thread, sequence));
                CREATE TABLE IF NOT EXISTS actions(
                    thread TEXT, call_id TEXT, digest TEXT NOT NULL, result TEXT,
                    PRIMARY KEY(thread, call_id));
                CREATE TABLE IF NOT EXISTS action_refusals(
                    thread TEXT NOT NULL, proposal TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(thread,proposal));
                CREATE TABLE IF NOT EXISTS incomplete_action_reviews(
                    thread TEXT NOT NULL, proposal TEXT NOT NULL, call_id TEXT NOT NULL, code TEXT NOT NULL,
                    concern_id INTEGER, PRIMARY KEY(thread,proposal));
                CREATE TABLE IF NOT EXISTS recovered_concern_questions(
                    concern_id INTEGER NOT NULL, version INTEGER NOT NULL, code TEXT NOT NULL, question TEXT NOT NULL,
                    PRIMARY KEY(concern_id,version));
                CREATE TABLE IF NOT EXISTS events(
                    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, thread TEXT NOT NULL,
                    event TEXT NOT NULL, tool TEXT, call_id TEXT);
                CREATE INDEX IF NOT EXISTS action_decision_events ON events(thread)
                    WHERE event IN ('opus_allowed','opus_denied','opus_asked_operator');
                CREATE TABLE IF NOT EXISTS luna_flags(
                    id INTEGER PRIMARY KEY,
                    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    thread TEXT NOT NULL, batch_id INTEGER, sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL, tool TEXT, call_id TEXT,
                    evidence TEXT NOT NULL, summary TEXT NOT NULL);
            """)
            concerns.initialize(db)

    def bind(self, thread: str) -> None:
        if not thread or (self.thread and self.thread != thread):
            raise ValueError("A review session must bind to exactly one thread")
        with database(self.path) as db:
            db.execute("INSERT OR IGNORE INTO threads VALUES (?, 0, 0)", (thread,))
        self.thread = thread

    def _event(self, event: str, tool=None, call_id=None) -> None:
        with database(self.path) as db:
            db.execute("INSERT INTO events(thread, event, tool, call_id) VALUES (?, ?, ?, ?)",
                       (self.thread, event, tool, call_id))

    def reviewed_calls(self):
        """Identify previously registered calls in this room, including denials."""
        with database(self.path) as db:
            return set(db.execute("""SELECT DISTINCT e.tool,e.call_id FROM events e
                JOIN actions a ON a.thread=e.thread AND a.call_id=e.call_id
                WHERE e.thread=? AND e.tool IS NOT NULL""", (self.thread,)))

    def _record_screen(self, db, scan, sequence, kind, tool=None, call_id=None):
        """Commit Luna's stated concern with its event, independently of the later ruling."""
        flagged = scan["escalate"]
        prefix = "unregistered" if kind == "unregistered_tool" else kind
        event = prefix + ("_flagged" if flagged else "_clear")
        db.execute("INSERT INTO events(thread,event,tool,call_id) VALUES (?,?,?,?)",
                   (self.thread, event, tool, call_id))
        if flagged:
            # Unregistered names and arguments originate on the wire. Store a
            # fixed label for them, alongside the two reviewed report fields.
            label = "unregistered" if kind == "unregistered_tool" else tool
            db.execute("""INSERT INTO luna_flags(thread,batch_id,sequence,kind,tool,call_id,evidence,summary)
                          VALUES (?,?,?,?,?,?,?,?)""",
                       (self.thread, self.batch_id, sequence, kind, label, call_id,
                        scan["evidence"], scan["summary"]))

    def _screen(self, packet) -> dict:
        try:
            answer = self.luna.screen(packet)
            if (type(answer["escalate"]) is not bool or not isinstance(answer["summary"], str)
                    or not isinstance(answer["evidence"], str)
                    or (answer["escalate"] and not answer["evidence"].strip())):
                raise ValueError("Invalid screening decision")
            return answer
        except Exception as exc:
            raise ReviewUnavailable("Luna review unavailable") from exc

    def observe(self, payload: dict) -> None:
        """Called before forwarding each Qwen request, including requests after tool results."""
        if self.thread is None:
            raise ReviewUnavailable("The supervisor has no bound thread")
        # A steering request may arrive while its predecessor is still screening.
        # Preserve arrival order and hold registered calls until the newest review.
        with self._observations:
            self._received += 1
            order = self._received
            self._current = None
            self._origin.context = None
            self._observations.wait_for(lambda: self._processed + 1 == order)
        try:
            self._origin.context = self._observe_context(payload)
        finally:
            with self._observations:
                if order == self._received:
                    self._current = self._origin.context
                self._processed = order
                self._observations.notify_all()

    def _observe_context(self, payload: dict) -> ReviewedContext:
        context = visible_context(payload)
        recent = recent_messages(context)
        with database(self.path) as db:
            db.execute("UPDATE threads SET requests=requests+1 WHERE thread=?", (self.thread,))
            sequence, sampled = db.execute("SELECT requests,sampled FROM threads WHERE thread=?",
                                           (self.thread,)).fetchone()
        if not sampled or sequence - sampled >= self.sample_every:
            with database(self.path) as db:
                history = concerns.history(db, self.thread)
                prior = db.execute("SELECT last_event FROM context_scan_cursors WHERE thread=?", (self.thread,)).fetchone()
                through = db.execute("SELECT COALESCE(MAX(rowid),0) FROM events WHERE thread=?", (self.thread,)).fetchone()[0]
                new_events = [] if not prior else db.execute("""SELECT rowid,tool,call_id FROM events
                    WHERE thread=? AND rowid>? AND rowid<=? AND event IN ('tool_flagged','tool_clear','unregistered_blocked')
                    ORDER BY rowid DESC LIMIT 41""", (self.thread, prior[0], through)).fetchall()
            packet = {"kind": "context", "context": context}
            if history:
                packet["concern_history"] = history
            facts = self._context_ruling_facts(history, recent)
            if facts:
                packet["host_facts"] = facts
            if new_events:
                packet["new_tool_events"] = [
                    {"id": event_id, "tool": tool, "call_id": call_id}
                    for event_id, tool, call_id in reversed(new_events[:40])]
                packet["earlier_tool_events_omitted"] = len(new_events) > 40
            try:
                scan = self._screen(packet)
                related = scan.get("repeat_id")
                if related is not None and (type(related) is not int or related not in {h["id"] for h in history}):
                    raise ReviewUnavailable("Luna returned an unknown concern reference")
            except ReviewUnavailable:
                self._event("context_unavailable")
                raise
            with database(self.path) as db:
                db.execute("UPDATE threads SET sampled=MAX(sampled,?) WHERE thread=?", (sequence, self.thread))
                db.execute("""INSERT INTO context_scan_cursors VALUES (?,?) ON CONFLICT(thread) DO UPDATE
                              SET last_event=MAX(last_event,excluded.last_event)""", (self.thread, through))
                finding = None
                if scan["escalate"]:
                    new_id = concerns.create(db, self.thread, sequence, scan["evidence"], scan["summary"], related)
                    finding = concerns.get(db, self.thread, new_id)
                elif related is not None:
                    concerns.repeat_seen(db, self.thread, sequence, related)
                    db.execute("INSERT INTO events(thread,event) VALUES (?, 'context_repeat')", (self.thread,))
                self._record_screen(db, scan, sequence, "context")
            if finding is not None:
                # No action has been proposed yet. A failed review remains pending for tool admission.
                try:
                    self._decide_concern(finding, {"recent_context": recent[-2400:]})
                except ReviewUnavailable:
                    self._event("context_review_unavailable")
        self._sync_concern_questions()
        return ReviewedContext(sequence, recent)

    def _retain_finding(self, sequence, evidence, summary):
        # A rejected proposal can arrive after the scan or another verdict on the same request.
        with database(self.path) as db:
            concerns.create(db, self.thread, sequence, evidence, summary)

    def unregistered(self, name, arguments) -> None:
        """Audit a forbidden wire proposal. No decision here can run a tool."""
        if self.thread is None:
            raise ReviewUnavailable("The supervisor has no reviewed request for this attempted tool")
        self._event("unregistered_blocked")
        # The gateway reports stream violations on the same HTTP handler that
        # observed that request, even after a newer model request has started.
        context = getattr(self._origin, "context", None)
        if context is None:
            self._event("unregistered_screen_unavailable")
            raise ReviewUnavailable("The supervisor has no reviewed request for this attempted tool")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                pass  # The stream may be rejected before complete arguments arrive.
        rationale = arguments.get("rationale") if isinstance(arguments, dict) else None
        packet = {"kind": "tool", "tool": name, "arguments": arguments, "rationale": rationale,
                  "recent_context": context.recent, "host_status": "unregistered tool blocked before dispatch; arguments may be partial"}
        try:
            scan = self._screen(packet)
        except ReviewUnavailable:
            self._event("unregistered_screen_unavailable")
            self._retain_finding(context.sequence, "An unregistered proposal was blocked before screening completed.",
                                 "The tool was unavailable and did not execute.")
            raise
        with database(self.path) as db:
            self._record_screen(db, scan, context.sequence, "unregistered_tool")
        if scan["escalate"]:
            case = {key: packet[key] for key in ("tool", "arguments", "rationale", "host_status")}
            case.update(evidence=scan["evidence"], summary=scan["summary"])
            try:
                verdict = self._judgment(case)
            except ReviewUnavailable as exc:
                self._event("unregistered_review_unreadable" if isinstance(exc, ReviewUnreadable)
                            else "unregistered_review_unavailable")
                self._retain_finding(context.sequence, scan["evidence"], scan["summary"])
                raise
            self._event("unregistered_opus_" + verdict["decision"])

    def dispatch(self, call_id: str, name: str, args: dict, execute) -> dict:
        """Handle one stable Codex call ID and only execute that exact reviewed proposal."""
        with self._observations:
            context = self._current
        if context is None:
            return {"error": "Tool held because its model request has not passed supervision."}
        if not isinstance(call_id, str) or not call_id or name not in self.tool_names or not isinstance(args, dict):
            return {"error": "Unregistered or malformed tool call."}
        proposal = json.dumps([name, args], sort_keys=True, allow_nan=False, separators=(",", ":"))
        digest = hashlib.sha256(proposal.encode()).hexdigest()
        with database(self.path) as db:
            inserted = db.execute("INSERT OR IGNORE INTO actions VALUES (?, ?, ?, NULL)",
                                  (self.thread, call_id, digest)).rowcount
            if not inserted:
                known, result = db.execute("SELECT digest,result FROM actions WHERE thread=? AND call_id=?",
                                           (self.thread, call_id)).fetchone()
                if known != digest:
                    return {"error": "A prior call used this ID with different arguments."}
                if result is None:
                    return {"error": "This action is in progress or its outcome is unknown. Check before retrying."}
                return json.loads(result)
        # A crash between execution and result persistence leaves the call claimed.
        # Never replay an ambiguous side effect from the same Codex call.
        feedback = {}
        try:
            result = self._review_action(context, call_id, name, json.loads(proposal)[1], execute, feedback)
        except ReviewUnreadable as exc:
            self._event("tool_review_unreadable", name, call_id)
            result = {"error": str(exc)}
        except ReviewUnavailable as exc:
            self._event("tool_review_unavailable", name, call_id)
            result = {"error": f"{exc}. The proposed tool action was held."}
        except ValueError:
            result = {"error": "Invalid tool arguments."}
        with database(self.path) as db:
            # A context-only review has no tool call of its own. Deliver one queued
            # reply per new result, atomically with that result's replay receipt.
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("""SELECT rowid,concern_id,batch_id,parsed_decision,response_text
                                    FROM pending_context_replies WHERE thread=? ORDER BY rowid LIMIT 1""",
                                 (self.thread,)).fetchone()
            if pending:
                feedback["opus_context_review"] = {"concern_id": pending[1], "turn": pending[2],
                                                   "parsed_decision": pending[3], "response_text": pending[4]}
                db.execute("DELETE FROM pending_context_replies WHERE rowid=?", (pending[0],))
            if feedback:
                result = {**result, **feedback}
            db.execute("UPDATE actions SET result=? WHERE thread=? AND call_id=?",
                       (json.dumps(result, allow_nan=False), self.thread, call_id))
        return result

    def _citation_facts(self, db, texts):
        references = concerns.cited_references(*texts)
        if not references:
            return {}
        return {"concern_citations": concerns.citation_receipts(db, self.thread, references, self.operator_room)}

    def _context_ruling_facts(self, items, recent=""):
        with database(self.path) as db:
            facts = self._citation_facts(db, [recent, *(item[key] for item in items
                                                      for key in ("reason", "evidence", "summary"))])
        if self.operator_room and items:
            ids = {item["id"] for item in items}
            releases = [row for row in self.operator_room.concern_releases(self.thread, CONSULT)
                        if row["concern_id"] in ids]
            if releases:
                facts["concern_releases"] = releases
        return facts

    def _ruling_facts(self, db, name, args, rationale, matching, prior, *review_texts):
        # Prioritize citations used to justify this review over numbers mentioned in a draft.
        texts = [*review_texts, prior, rationale, *(item[key] for item in matching
                                                   for key in ("reason", "evidence", "summary")), *args.values()]
        facts = self._citation_facts(db, texts)
        ids, recent = ruling_query(name, args, rationale)
        if ids or recent:
            facts["related_rulings"] = concerns.review_receipts(db, self.thread, ids, include_recent=recent)
        if recent:
            decisions = {"opus_allowed": "allow", "opus_denied": "deny", "opus_asked_operator": "ask_operator"}
            rows = db.execute("""SELECT event,tool,call_id,at FROM events WHERE thread=?
                AND event IN ('opus_allowed','opus_denied','opus_asked_operator')
                ORDER BY rowid DESC LIMIT 3""", (self.thread,))
            facts["recent_action_decisions"] = [
                {"tool": tool, "call_id": call_id, "decision": decisions[event], "at_utc": at}
                for event, tool, call_id, at in rows if call_id and len(call_id) <= 160]
        return facts

    def _review_action(self, context, call_id, name, args, execute, feedback):
        rationale = args.pop("rationale", None)
        with database(self.path) as db:
            matching = concerns.list_open(db, self.thread, name)
            prior = db.execute("SELECT reason FROM action_refusals WHERE thread=? AND proposal=?",
                               (self.thread, _fingerprint([name, args]))).fetchone()
            ruling_facts = self._ruling_facts(db, name, args, rationale, matching, prior[0] if prior else None)
        room = self.operator_room
        facts = action_facts(name, args,
                             operator_statements=room.statements(self.thread, self.batch_id) if room else [],
                             concern_releases=room.concern_releases(self.thread, name) if room else [],
                             open_concerns=matching, prior_denial=prior[0] if prior else None,
                             archive_receipt=room.archive_receipt(self.thread)
                             if room and name in ("read_heritage", "view_pdf_page") else None)
        facts.update(ruling_facts)
        if name == "workspace_write" and self.workspace_root is not None:
            facts["workspace_edit"] = workspace_edit_evidence(self.workspace_root, args)
        if name == OPEN_DRAFT:
            # Resolve the ID outside the agent's arguments. Both reviewers see this exact
            # immutable document, and the broker verifies its digest again at publication.
            try:
                facts["public_draft"] = self.draft_evidence(args) if self.draft_evidence is not None else None
            except (OSError, ValueError):
                facts["public_draft"] = None
        packet = {"kind": "tool", "host_facts": facts, "tool": name, "arguments": args,
                  "rationale": rationale, "recent_context": context.recent}
        # Only a version actually inspected and found irrelevant may skip Opus.
        # Pending, broad, and excess scopes remain applicable by default.
        checks = [item for item in matching if item["status"] != "pending" and "*" not in item["tools"]][:12]
        if checks:
            packet["scope_checks"] = checks
        scan = self._screen(packet)
        skipped = set()
        if checks:
            ids = scan.get("applicable_ids")
            known = {item["id"] for item in checks}
            if (not isinstance(ids, list) or any(type(i) is not int or i not in known for i in ids)
                    or len(set(ids)) != len(ids)):
                raise ReviewUnavailable("Luna returned an invalid scope assessment")
            skipped = {(i["id"], i["version"]) for i in checks if i["id"] not in ids}
        with database(self.path) as db:
            self._record_screen(db, scan, context.sequence, "tool", name, call_id)
        if name == OPEN_DRAFT and facts["public_draft"] is None:
            raise ReviewUnavailable("The host could not verify the full public patch and destination for this proposal ID")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > RATIONALE_LIMIT:
            if scan["escalate"]:
                self._retain_finding(context.sequence, scan["evidence"], scan["summary"])
            return {"error": f"Provide a nonempty public rationale of at most {RATIONALE_LIMIT} characters for this action."}
        case = {"host_facts": facts, "tool": name, "arguments": args, "rationale": rationale,
                "evidence": scan["evidence"], "summary": scan["summary"]}
        held = self._adjudicate(call_id, case, scan["escalate"], skipped, feedback)
        if held is not None:
            return held
        if name == CONSULT:
            if (set(args) != {"question"} or not isinstance(args["question"], str)
                    or not args["question"].strip() or len(args["question"]) > 2400):
                return {"error": "Provide one nonempty question of at most 2400 characters."}
            try:
                report = self.consult(case)
                if not isinstance(report, dict):
                    raise OpusResponseError("invalid_advice")
                _save_reply_text(feedback, "opus_advice_text", report.get("response_text"))
                advice, research, sources = (report.get(k) for k in ("advice", "research", "available_research"))
                if (not isinstance(advice, str) or not advice.strip() or not isinstance(research, list)
                        or sources != ["wikipedia", "crossref"]
                        or not all(isinstance(r, dict) and r.get("source") in sources
                                   and r.get("status") in ("ok", "unavailable") for r in research)):
                    raise OpusResponseError("invalid_advice")
            except Exception as exc:
                if isinstance(exc, OpusResponseError):
                    _save_reply_text(feedback, "opus_advice_text", exc.response_text)
                raise _opus_failure(exc, batch_id=self.batch_id, consultation=True) from exc
            self._event("opus_advice", name, call_id)
            return {"advice": advice, "research": research, "available_research": sources}
        return execute(name, args)

    def _case_with_concerns(self, items, action=None):
        case = dict(action or {})
        room = self.operator_room
        if "tool" not in case:
            facts = self._context_ruling_facts(items, case.get("recent_context", ""))
            if facts:
                case["host_facts"] = facts
        if room and "operator_statements" not in case:
            case["operator_statements"] = room.statements(self.thread, self.batch_id)
        records = []
        attestations = []
        for item in items:
            record = {**item, "scope_tools": sorted(self.tool_names)}
            related = self._related_receipt(item)
            if related:
                if len(items) == 1:
                    case["related_concern_receipt"] = related
                else:
                    record["related_concern_receipt"] = related
            records.append(record)
            if room:
                prior = room.case(self.thread, f"context-concern:{item['id']}")
                if prior and prior["answer"]:
                    attestations.append({"concern_id": item["id"], "operator_attestation": _attestation(prior)})
                if len(items) == 1:
                    case["concern_question_available"] = prior is None and "operator_attestation" not in case
        if len(items) == 1:
            case["concern_review"] = records[0]
            if attestations:
                case["prior_concern_attestation"] = attestations[0]["operator_attestation"]
        else:
            case["concern_reviews"] = records
            if attestations:
                case["prior_concern_attestations"] = attestations
        return case

    def _related_receipt(self, item):
        if item["related_id"] is not None:
            with database(self.path) as db:
                prior = concerns.get(db, self.thread, item["related_id"], include_action=True)
            if prior:
                receipt = {
                    key: prior[key] for key in ("id", "sequence", "version", "status", "tools")}
                for key in ("evidence", "reason"):
                    value = prior[key]
                    receipt[key] = value if len(value) <= 600 else value[:599] + "…"
                if "action" in prior:
                    receipt["action"] = prior["action"]
                return receipt

    def _record_ruling(self, item, ruling):
        with database(self.path) as db:
            if not concerns.record(db, self.thread, item["id"], item["version"], ruling["decision"],
                                   ruling["reason"], ruling["tools"]):
                raise ReviewUnavailable("The concern changed during review")
        if ruling["decision"] == "ask_operator":
            self._concern_question(item, ruling["reason"])

    def _concern_question(self, item, question):
        scope = f"context-concern:{item['id']}"
        row = self.operator_room.case(self.thread, scope)
        if row is None:
            excerpt = item["evidence"][:2000]
            with database(self.path) as db:
                recovered = db.execute("SELECT code,question FROM recovered_concern_questions "
                                       "WHERE concern_id=? AND version=?", (item["id"], item["version"])).fetchone()
            if recovered:
                code, question = recovered
                excerpt = recovered_excerpt(OpusResponseError(code), excerpt)
            self.operator_room.open(self.thread, scope, "context_concern", "host:" + scope,
                                    f"Context concern #{item['id']}", excerpt, question)
            row = self.operator_room.case(self.thread, scope)
        return row

    def _recover_concern_question(self, item, error):
        # Move the existing host scope into a waiting state. The new question is
        # evidence for Mike, never a replacement for the previous watch criteria.
        with database(self.path) as db:
            if not concerns.record(db, self.thread, item["id"], item["version"], "ask_operator",
                                   item["reason"] or item["evidence"], item["tools"] or ["*"]):
                raise ReviewUnavailable("The concern changed during review")
            version = item["version"] + 1
            db.execute("INSERT INTO recovered_concern_questions VALUES (?,?,?,?)",
                       (item["id"], version, error.code, error.concern_question))
        return self._concern_question({**item, "version": version}, error.concern_question)

    def _decide_concern(self, item, action=None):
        case = self._case_with_concerns([item], action)
        try:
            verdict = self._context_judgment(case)
        except ReviewUnreadable as exc:
            error = exc.__cause__
            if case.get("concern_question_available") and error.concern_question:
                self._recover_concern_question(item, error)
            raise
        self._record_ruling(item, verdict["concern"])
        return verdict

    def _context_judgment(self, case):
        feedback = {}
        decision = None
        try:
            verdict = self._judgment(case, feedback)
            decision = verdict["concern"]["decision"]
            return verdict
        finally:
            if "opus_review_text" in feedback:
                with database(self.path) as db:
                    db.execute("""INSERT INTO pending_context_replies
                                  (thread,concern_id,batch_id,parsed_decision,response_text) VALUES (?,?,?,?,?)""",
                               (self.thread, case["concern_review"]["id"], self.batch_id, decision,
                                feedback["opus_review_text"]))

    def _sync_concern_questions(self):
        room = self.operator_room
        if room is None:
            return
        with database(self.path) as db:
            questions = [f for f in concerns.list_open(db, self.thread) if f["status"] == "question"]
        for item in questions:
            row = self._concern_question(item, item["reason"])
            if row["status"] != "answered" or row["final_decision"] == "unavailable":
                continue
            # The operator receipt commits first. On restart, apply its stored
            # disposition without paying again or interpreting the prose as a ruling.
            key = _fingerprint([item["id"], item["version"], row["id"]])
            if not row["final_decision"]:
                if room.claim_final(row["id"], key) != "claimed":
                    continue
                try:
                    case = self._case_with_concerns([item])
                    case.pop("prior_concern_attestation", None)
                    case["concern_attestation"] = _attestation(row)
                    ruling = self._context_judgment(case)["concern"]
                except ReviewUnavailable as exc:
                    if isinstance(exc.__cause__, (ReviewBudgetUnavailable, OpusCaseTooLarge)):
                        room.release_final(row["id"], key)
                    else:
                        room.finish_final(row["id"], key, "unavailable", str(exc))
                    continue
                room.finish_final(row["id"], key, ruling["decision"], ruling["reason"], tools=ruling["tools"])
            else:
                ruling = {"decision": row["final_decision"], "reason": row["final_reason"],
                          "tools": json.loads(row["final_tools"])}
            self._record_ruling(item, ruling)

    def _adjudicate(self, call_id, case, flagged, skipped, feedback):
        """Return a tool error for a held action, or None to proceed with its dispatch."""
        name, args = case["tool"], case["arguments"]
        room = self.operator_room
        scope = question_scope(name, args) if room else None
        prior = room.case(self.thread, scope) if room else None
        # Resolve the action's own operator question independently of context rulings.
        if prior and prior["status"] == "pending":
            return self._waiting(prior["id"], prior["question"])
        if prior and not prior["final_decision"] and prior["final_fingerprint"]:
            return {"error": f"The final review for question #{prior['id']} is in progress or its outcome is unknown. "
                             "Leave the action held and report it to Mike."}
        if room:
            case["operator_statements"] = case["host_facts"]["operator_statements"]
        needs_final = prior is not None and not prior["final_decision"]
        if prior:
            # New operator statements can change the evidence after a refusal.
            # Merely rephrasing the public rationale leaves this fingerprint unchanged.
            fingerprint_parts = [name, args, [s["event_id"] for s in case["operator_statements"]]]
            releases = case["host_facts"]["concern_releases"]
            if releases:
                fingerprint_parts.append([r["receipt_event_id"] for r in releases])
            fingerprint = _fingerprint(fingerprint_parts)
            if prior["final_fingerprint"] == fingerprint and prior["final_decision"] in ("deny", "unavailable"):
                return {"error": f"Review #{prior['id']} held this proposal. Mike can supply new information.",
                        "judgment": prior["final_reason"]}
            case["operator_attestation" if needs_final else "prior_operator_attestation"] = _attestation(prior)
        with database(self.path) as db:
            proposal_key = _fingerprint([name, args])
            refusal = db.execute("SELECT reason FROM action_refusals WHERE thread=? AND proposal=?",
                                 (self.thread, proposal_key)).fetchone()
            incomplete = db.execute("SELECT call_id,code,concern_id FROM incomplete_action_reviews "
                                    "WHERE thread=? AND proposal=?", (self.thread, proposal_key)).fetchone()
            matching = concerns.list_open(db, self.thread, name)
            ruling_facts = self._ruling_facts(db, name, args, case["rationale"], matching,
                                              refusal[0] if refusal else None, case["evidence"], case["summary"],
                                              prior["final_reason"] if prior else None)
            findings = [f for f in matching
                        if (f["id"], f["version"]) not in skipped
                        or (incomplete and f["id"] == incomplete[2] and f["status"] == "question")]
        # A concurrent ruling can change the ledger during Luna's call. Keep Opus's
        # factual record current and recheck the versions when committing its verdict.
        previous = {key: value for key, value in case["host_facts"].items() if key != "concern_citations"}
        case["host_facts"] = {**previous, "prior_exact_denial": refusal[0] if refusal else None,
                              "open_concerns_for_tool": concern_refs(matching), **ruling_facts}
        if refusal:
            case["prior_action_denial"] = refusal[0]
        if incomplete:
            case["prior_incomplete_review"] = incomplete[1]
            if self.operator_room and incomplete[2] is not None:
                question = self.operator_room.case(self.thread, f"context-concern:{incomplete[2]}")
                if question and question["answer"]:
                    case["prior_concern_attestation"] = _attestation(question)
        if not (findings or flagged or refusal or incomplete) and (prior is None or prior["final_decision"] == "allow"):
            return None
        # No paid decision can clear a pre-existing applicable operator question.
        for item in findings:
            if item["status"] == "question":
                if not self.operator_room:
                    return {"error": "An applicable context concern is awaiting the private operator room."}
                return self._concern_hold(self._concern_question(item, item["reason"]))
        if room and prior is None:
            case["operator_question_available"] = True
        batch = self._case_with_concerns(findings, case) if findings else case
        if len(findings) > 1 and not incomplete:
            # A malformed, stale, or interrupted combined reply grants no partial
            # permission, even if those watches are subsequently closed elsewhere.
            incomplete = (call_id, "pending_concern_batch", None)
            with database(self.path) as db:
                if not db.execute("INSERT OR IGNORE INTO incomplete_action_reviews VALUES(?,?,?,?,?)",
                                  (self.thread, proposal_key, *incomplete)).rowcount:
                    raise ReviewUnavailable("The proposal already has an incomplete review. Submit a fresh assessment")
        if needs_final and room.claim_final(prior["id"], fingerprint) != "claimed":
            raise ReviewUnavailable(f"The final review for question #{prior['id']} was already claimed")
        try:
            verdict = self._judgment(batch, feedback)
            rulings = verdict.get("concerns", []) if len(findings) != 1 else [verdict["concern"]]
            self._apply_action_verdict(call_id, name, proposal_key, findings, skipped, refusal, incomplete, verdict, rulings)
        except ReviewUnavailable as exc:
            if needs_final:
                if isinstance(exc.__cause__, (ReviewBudgetUnavailable, OpusCaseTooLarge)):
                    room.release_final(prior["id"], fingerprint)
                else:
                    room.finish_final(prior["id"], fingerprint, "unavailable", str(exc))
            elif isinstance(exc, ReviewUnreadable):
                held = self._recover_action_question(exc.__cause__, batch, call_id, proposal_key, scope)
                if held is not None:
                    return held
                if exc.__cause__.code == "indirect_operator_request":
                    with database(self.path) as db:
                        db.execute("INSERT OR IGNORE INTO incomplete_action_reviews VALUES(?,?,?,?,NULL)",
                                   (self.thread, proposal_key, call_id, exc.__cause__.code))
            raise
        if needs_final:
            room.finish_final(prior["id"], fingerprint, verdict["decision"], verdict["reason"])
        concern_hold = None
        for item, ruling in zip(findings, rulings, strict=True):
            if ruling["decision"] == "ask_operator":
                question = self._concern_question(item, ruling["reason"])
                if name in ruling["tools"] or "*" in ruling["tools"]:
                    concern_hold = self._concern_hold(question)
        if verdict["decision"] == "deny":
            self._event("opus_denied", name, call_id)
            return {"error": "Claude 3 Opus denied this action.", "judgment": verdict["reason"]}
        if verdict["decision"] == "ask_operator":
            question_id = room.open(self.thread, scope, name, call_id, describe_action(name, args),
                                    _review_excerpt(batch), verdict["reason"])
            self._event("opus_asked_operator", name, call_id)
            return self._waiting(question_id, verdict["reason"])
        if concern_hold:
            return concern_hold
        self._event("opus_allowed", name, call_id)
        return None

    def _recover_action_question(self, error, batch, call_id, proposal, scope):
        action_question = batch.get("operator_question_available") and error.action_question
        concern_question = batch.get("concern_question_available") and error.concern_question
        if not (action_question or concern_question):
            return None
        item = batch.get("concern_review")
        name = batch["tool"]
        with database(self.path) as db:
            db.execute("INSERT OR REPLACE INTO incomplete_action_reviews VALUES(?,?,?,?,?)",
                       (self.thread, proposal, call_id, error.code, None if action_question else item["id"]))
        self._event("tool_review_unreadable", name, call_id)
        if action_question:
            self.operator_room.open(self.thread, scope, name, call_id, describe_action(name, batch["arguments"]),
                                    recovered_excerpt(error, _review_excerpt(batch)), action_question)
            row = self.operator_room.case(self.thread, scope)
        else:
            row = self._recover_concern_question(item, error)
        held = self._concern_hold(row)
        if error.code == "indirect_operator_request":
            held["error"] = "The host redirected Opus's clarification to Mike. " + held["error"]
        else:
            held["error"] = f"Opus's ruling could not be validated ({error.code}). " + held["error"]
        return held

    def _apply_action_verdict(self, call_id, name, proposal, findings, skipped, refusal, incomplete, verdict, rulings):
        """Commit every matching concern disposition and the proposal decision atomically."""
        reviewed = skipped | {(item["id"], item["version"]) for item in findings}
        with database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if any((item["id"], item["version"]) not in reviewed for item in concerns.list_open(db, self.thread, name)):
                raise ReviewUnavailable("A context concern changed during review. The proposal needs a fresh assessment")
            latest = db.execute("SELECT call_id,code,concern_id FROM incomplete_action_reviews WHERE thread=? AND proposal=?",
                                (self.thread, proposal)).fetchone()
            current_refusal = db.execute("SELECT reason FROM action_refusals WHERE thread=? AND proposal=?",
                                         (self.thread, proposal)).fetchone()
            if latest != incomplete or current_refusal != refusal:
                raise ReviewUnavailable("The proposal's prior review changed. The proposal needs a fresh assessment")
            for item, ruling in zip(findings, rulings, strict=True):
                if not concerns.record(db, self.thread, item["id"], item["version"], ruling["decision"],
                                       ruling["reason"], ruling["tools"]):
                    raise ReviewUnavailable("A context concern changed during review. The proposal needs a fresh assessment")
                concerns.link_action(db, item["id"], item["version"] + 1, tool=name, call_id=call_id,
                                     decision=verdict["decision"], reason=verdict["reason"], turn=self.batch_id)
            if verdict["decision"] == "deny":
                db.execute("INSERT OR REPLACE INTO action_refusals VALUES(?,?,?)",
                           (self.thread, proposal, verdict["reason"]))
            elif verdict["decision"] == "ask_operator":
                # If the process stops before the room saves the popup, a retry
                # still needs a valid ruling even when this reply retired all watches.
                db.execute("INSERT OR REPLACE INTO incomplete_action_reviews VALUES(?,?,?,?,NULL)",
                           (self.thread, proposal, call_id, "pending_operator_question"))
                return
            for ruling in rulings:
                if ruling["decision"] == "ask_operator" and ("*" in ruling["tools"] or name in ruling["tools"]):
                    db.execute("INSERT OR REPLACE INTO incomplete_action_reviews VALUES(?,?,?,?,?)",
                               (self.thread, proposal, call_id, "pending_concern_question", ruling["id"]))
                    return
            if verdict["decision"] == "allow" and refusal:
                db.execute("DELETE FROM action_refusals WHERE thread=? AND proposal=?", (self.thread, proposal))
            if incomplete:
                db.execute("DELETE FROM incomplete_action_reviews WHERE thread=? AND proposal=?", (self.thread, proposal))

    def _concern_hold(self, row):
        if row["status"] == "pending":
            return self._waiting(row["id"], row["question"])
        return {"error": f"Context review #{row['id']} has an operator answer but no confirmed disposition. "
                         "Leave the action held and report it to Mike.", "review_id": row["id"]}

    @staticmethod
    def _waiting(question_id, question):
        return {"error": f"Action held for Mike's direct answer to review #{question_id} in the private web room. "
                         "Leave this action pending. The host will notify you when Mike answers.",
                "review_id": question_id, "question": question}

    def _judgment(self, case, feedback=None):
        try:
            verdict = self.review(case)
            if not isinstance(verdict, dict):
                raise OpusResponseError("invalid_verdict")
            _save_reply_text(feedback, "opus_review_text", verdict.get("response_text"))
            if "concern_review" in case:
                items, rulings = [case["concern_review"]], [verdict.get("concern")]
            else:
                items, rulings = case.get("concern_reviews", []), verdict.get("concerns", [])
            if not isinstance(rulings, list) or len(items) != len(rulings):
                raise OpusResponseError("invalid_concern_ruling")
            for item, ruling in zip(items, rulings, strict=True):
                if (not isinstance(ruling, dict) or type(ruling.get("id")) is not int or ruling["id"] != item["id"]
                        or ruling.get("decision") not in ("retire", "watch", "ask_operator")
                        or not isinstance(ruling.get("reason"), str) or not ruling["reason"].strip()
                        or not isinstance(ruling.get("tools"), list)):
                    raise OpusResponseError("invalid_concern_ruling")
                tools = ruling["tools"]
                if (bool(tools) != (ruling["decision"] != "retire") or
                        tools != ["*"] and (any(not isinstance(t, str) or t not in self.tool_names for t in tools)
                                            or len(tools) != len(set(tools)))):
                    raise OpusResponseError("invalid_concern_scope")
                if ruling["decision"] == "ask_operator" and (not case.get("concern_question_available")
                        or len(items) != 1 or "concern_attestation" in case or len(ruling["reason"]) > OPERATOR_QUESTION_LIMIT):
                    raise OpusResponseError("invalid_concern_question")
            if items and "tool" not in case:
                return verdict
            accepts_question = (case.get("operator_question_available") is True and
                                "operator_attestation" not in case and self.operator_room is not None)
            decisions = ("allow", "deny", "ask_operator") if accepts_question else ("allow", "deny")
            if (not isinstance(verdict, dict) or verdict.get("decision") not in decisions
                    or not isinstance(verdict.get("reason"), str) or not verdict["reason"].strip()):
                raise OpusResponseError("invalid_verdict")
            if verdict["decision"] == "ask_operator" and len(verdict["reason"]) > OPERATOR_QUESTION_LIMIT:
                raise OpusResponseError("invalid_verdict")
            if (verdict["decision"] == "ask_operator" and
                    verdict.get("concern", {}).get("decision") == "ask_operator"):
                raise OpusResponseError("invalid_concern_question")
            return verdict
        except Exception as exc:
            if isinstance(exc, OpusResponseError):
                _save_reply_text(feedback, "opus_review_text", exc.response_text)
            raise _opus_failure(exc, batch_id=self.batch_id) from exc
