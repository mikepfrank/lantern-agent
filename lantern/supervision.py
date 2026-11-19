"""Review Lantern's proposed actions before dispatching any tool."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Condition, local
from typing import NamedTuple


CONSULT = "consult_opus"
TOOLS = {"chess_analyze", "message_operator", "read_heritage", CONSULT}
RATIONALE_LIMIT = 1200
DATABASE = "supervision.sqlite"


class ReviewUnavailable(RuntimeError):
    """Required review could not complete. A request must remain unexecuted."""


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
    excerpts = []
    for item in reversed(items):
        if item.get("type") == "function_call_output":
            role, content = "tool result", item.get("output", "")
        elif item.get("role") in ("assistant", "user"):
            role, content = item["role"], item.get("content", "")
        else:
            continue
        if isinstance(content, list):
            content = "\n".join(p["text"] for p in content if "text" in p)
        excerpts.append(f"{role}: {content[-1200:]}")
        if len(excerpts) == 4:
            break
    return "\n".join(reversed(excerpts))


def review_cases(case, findings):
    # Opus accepts 64 KiB cases. Preserve each unresolved finding; a long
    # thread can accrue many full-window reports before its next real tool call.
    base_size = len(json.dumps(case, ensure_ascii=False).encode())
    limit = 60_000
    packet, sequence, size = [], [], base_size
    for number, evidence, summary in findings:
        concern = {"evidence": evidence, "summary": summary}
        item_size = len(json.dumps(concern, ensure_ascii=False).encode()) + 2
        if packet and size + item_size > limit:
            yield {**case, "context_concern": packet}, sequence
            packet, sequence, size = [], [], base_size
        packet.append(concern)
        sequence.append(number)
        size += item_size
    yield {**case, "context_concern": packet}, sequence


class Supervisor:
    def __init__(self, state: Path, luna, review, consult, *, sample_every: int = 12, tool_names=None):
        if type(sample_every) is not int or sample_every < 1:
            raise ValueError("Sample interval must be a positive integer")
        self.path = state / DATABASE
        self.luna, self.review, self.consult = luna, review, consult
        self.sample_every = sample_every
        self.tool_names = frozenset(TOOLS if tool_names is None else tool_names)
        self.thread = None
        self._observations = Condition()
        self._received = self._processed = 0
        self._current = None
        self._origin = local()
        with database(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS threads(
                    thread TEXT PRIMARY KEY, requests INTEGER NOT NULL, sampled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS findings(
                    thread TEXT, sequence INTEGER, evidence TEXT NOT NULL, summary TEXT NOT NULL,
                    heard INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(thread, sequence));
                CREATE TABLE IF NOT EXISTS actions(
                    thread TEXT, call_id TEXT, digest TEXT NOT NULL, result TEXT,
                    PRIMARY KEY(thread, call_id));
                CREATE TABLE IF NOT EXISTS events(
                    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, thread TEXT NOT NULL,
                    event TEXT NOT NULL, tool TEXT, call_id TEXT);
            """)

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
        with database(self.path) as db:
            db.execute("UPDATE threads SET requests=requests+1 WHERE thread=?", (self.thread,))
            sequence, sampled = db.execute("SELECT requests,sampled FROM threads WHERE thread=?",
                                           (self.thread,)).fetchone()
        if not sampled or sequence - sampled >= self.sample_every:
            try:
                scan = self._screen({"kind": "context", "context": context})
            except ReviewUnavailable:
                self._event("context_unavailable")
                raise
            with database(self.path) as db:
                db.execute("UPDATE threads SET sampled=MAX(sampled,?) WHERE thread=?", (sequence, self.thread))
                if scan["escalate"]:
                    db.execute("INSERT INTO findings(thread,sequence,evidence,summary) VALUES (?,?,?,?)",
                               (self.thread, sequence, scan["evidence"], scan["summary"]))
                else:
                    # Only clear issues Opus has already considered on a concrete action.
                    db.execute("DELETE FROM findings WHERE thread=? AND sequence<? AND heard=1",
                               (self.thread, sequence))
            self._event("context_flagged" if scan["escalate"] else "context_clear")
        return ReviewedContext(sequence, recent_messages(context))

    def _retain_finding(self, sequence, evidence, summary):
        # A context scan and a rejected proposal can both flag the same request.
        with database(self.path) as db:
            db.execute("""
                INSERT INTO findings(thread,sequence,evidence,summary) VALUES (?,?,?,?)
                ON CONFLICT(thread,sequence) DO UPDATE SET
                    evidence=findings.evidence || char(10) || excluded.evidence,
                    summary=findings.summary || char(10) || excluded.summary, heard=0
                """, (self.thread, sequence, evidence, summary))

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
        self._event("unregistered_flagged" if scan["escalate"] else "unregistered_clear")
        if scan["escalate"]:
            case = {key: packet[key] for key in ("tool", "arguments", "rationale", "host_status")}
            case.update(evidence=scan["evidence"], summary=scan["summary"])
            try:
                verdict = self._judgment(case)
            except ReviewUnavailable:
                self._event("unregistered_review_unavailable")
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
        try:
            result = self._review_action(context, call_id, name, json.loads(proposal)[1], execute)
        except ReviewUnavailable as exc:
            self._event("tool_review_unavailable", name, call_id)
            result = {"error": f"{exc}. The proposed tool action was held."}
        except ValueError:
            result = {"error": "Invalid tool arguments."}
        with database(self.path) as db:
            db.execute("UPDATE actions SET result=? WHERE thread=? AND call_id=?",
                       (json.dumps(result, allow_nan=False), self.thread, call_id))
        return result

    def _review_action(self, context, call_id, name, args, execute):
        rationale = args.pop("rationale", None)
        packet = {"kind": "tool", "tool": name, "arguments": args, "rationale": rationale,
                  "recent_context": context.recent}
        scan = self._screen(packet)
        self._event("tool_flagged" if scan["escalate"] else "tool_clear", name, call_id)
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > RATIONALE_LIMIT:
            if scan["escalate"]:
                self._retain_finding(context.sequence, scan["evidence"], scan["summary"])
            return {"error": f"Provide a nonempty public rationale of at most {RATIONALE_LIMIT} characters for this action."}
        with database(self.path) as db:
            findings = db.execute("SELECT sequence,evidence,summary FROM findings WHERE thread=? ORDER BY sequence",
                                  (self.thread,)).fetchall()
        case = {"tool": name, "arguments": args, "rationale": rationale,
                "evidence": scan["evidence"], "summary": scan["summary"]}
        if scan["escalate"] or findings:
            for batch, sequences in review_cases(case, findings):
                verdict = self._judgment(batch)
                if sequences:
                    with database(self.path) as db:
                        # A delayed proposal can append evidence while Opus is
                        # reviewing. Mark only the exact evidence it received.
                        db.executemany("UPDATE findings SET heard=1 WHERE thread=? AND sequence=? AND evidence=? AND summary=?",
                                       [(self.thread, i, item["evidence"], item["summary"])
                                        for i, item in zip(sequences, batch["context_concern"])])
                if verdict["decision"] == "deny":
                    self._event("opus_denied", name, call_id)
                    return {"error": "Claude 3 Opus denied this action.", "judgment": verdict["reason"]}
            self._event("opus_allowed", name, call_id)
        if name == CONSULT:
            if (set(args) != {"question"} or not isinstance(args["question"], str)
                    or not args["question"].strip() or len(args["question"]) > 2400):
                return {"error": "Provide one nonempty question of at most 2400 characters."}
            try:
                report = self.consult(case)
                advice, research, sources = (report[k] for k in ("advice", "research", "available_research"))
                if (not isinstance(advice, str) or not advice.strip() or not isinstance(research, list)
                        or sources != ["wikipedia", "crossref"]
                        or not all(isinstance(r, dict) and r.get("source") in sources
                                   and r.get("status") in ("ok", "unavailable") for r in research)):
                    raise ValueError("Invalid Opus advice or research report")
            except Exception as exc:
                raise ReviewUnavailable("Opus consultation unavailable") from exc
            self._event("opus_advice", name, call_id)
            return {"advice": advice, "research": research, "available_research": sources}
        return execute(name, args)

    def _judgment(self, case):
        try:
            verdict = self.review(case)
            if (verdict["decision"] not in ("allow", "deny") or not isinstance(verdict["reason"], str)
                    or not verdict["reason"].strip()):
                raise ValueError("Invalid Opus decision")
            return verdict
        except Exception as exc:
            raise ReviewUnavailable("Opus review unavailable") from exc
