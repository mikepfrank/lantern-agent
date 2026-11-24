"""Private operator questions and attestations for the bound chat room."""

import json

from .chat_store import ChatStore
from .concerns import applicable_scopes, validate_scope
from .review_policy import ARCHIVE_REVIEW_ID


MAX_ANSWER = 2000
MAX_STATEMENT = 1200


def _text(value, limit, label, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or not (empty or value.strip()):
        raise ValueError(f"Invalid {label}")
    value.encode("utf-8")
    return value


class ReviewRoom:
    def __init__(self, store: ChatStore):
        self.store = store
        with store._connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operator_reviews (
                    id INTEGER PRIMARY KEY, thread TEXT NOT NULL, scope TEXT NOT NULL,
                    tool TEXT NOT NULL, call_id TEXT NOT NULL, action TEXT NOT NULL,
                    excerpt TEXT NOT NULL, question TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('opus','prior_denial')), asked_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','answered')),
                    answer TEXT, answered_at TEXT, wake_id INTEGER UNIQUE REFERENCES events(id),
                    final_fingerprint TEXT, final_decision TEXT, final_reason TEXT, final_tools TEXT,
                    UNIQUE(thread,call_id))""")
            if "final_tools" not in {row["name"] for row in db.execute("PRAGMA table_info(operator_reviews)")}:
                db.execute("ALTER TABLE operator_reviews ADD COLUMN final_tools TEXT")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS pending_operator_scope
                       ON operator_reviews(thread,scope) WHERE status='pending'""")
            db.execute("CREATE INDEX IF NOT EXISTS operator_review_history ON operator_reviews(thread,scope,id)")

    def pending(self):
        """Return only fields for displaying questions in the current room."""
        with self.store._connect() as db:
            rows = db.execute("""SELECT q.id,q.tool,q.action,q.excerpt,q.question,q.source,q.asked_at
                FROM operator_reviews q JOIN room r ON r.thread_id=q.thread
                WHERE q.status='pending' ORDER BY q.id""")
            return [dict(row) for row in rows]

    def open(self, thread, scope, tool, call_id, action, excerpt, question, source="opus"):
        """Create one question for this scope, even across concurrent calls or restarts."""
        _text(thread, 256, "room thread")
        _text(scope, 4096, "review scope")
        _text(tool, 128, "tool name")
        _text(call_id, 512, "tool call ID")
        _text(action, 2000, "action description")
        _text(excerpt, 2000, "excerpt", empty=True)
        _text(question, 1200, "question")
        if source not in ("opus", "prior_denial"):
            raise ValueError("Unknown question source")
        with self.store._connect(write=True) as db:
            if not db.execute("SELECT 1 FROM room WHERE id=1 AND thread_id=?", (thread,)).fetchone():
                raise ValueError("The review is outside the operator room")
            original = db.execute("SELECT id,scope FROM operator_reviews WHERE thread=? AND call_id=?",
                                  (thread, call_id)).fetchone()
            if original is not None:
                if original["scope"] != scope:
                    raise ValueError("Tool call already has a different review scope")
                return original["id"]
            if tool == "context_concern" and self._released(db, thread, scope):
                raise ValueError("The operator has already retired this context concern")
            pending = db.execute("""SELECT id FROM operator_reviews
                WHERE thread=? AND scope=? AND status='pending'""", (thread, scope)).fetchone()
            if pending is not None:
                return pending["id"]
            return db.execute("""INSERT INTO operator_reviews
                (thread,scope,tool,call_id,action,excerpt,question,source,asked_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (thread, scope, tool, call_id, action, excerpt,
                                               question, source, self.store.clock().isoformat())).lastrowid

    def case(self, thread, scope):
        with self.store._connect() as db:
            row = db.execute("""SELECT q.* FROM operator_reviews q JOIN room r ON r.thread_id=q.thread
                WHERE q.thread=? AND q.scope=? ORDER BY q.id DESC LIMIT 1""", (thread, scope)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _released(db, thread, scope):
        return db.execute("""SELECT 1 FROM operator_concern_releases
            WHERE thread=? AND 'context-concern:' || concern_id=?""", (thread, scope)).fetchone() is not None

    def _release_receipts(self, thread, selector, parameters):
        """Only this room's committed operator command and host receipt attest a release."""
        with self.store._connect() as db:
            rows = db.execute(f"""SELECT x.concern_id,x.reason,x.released_at,x.command_event_id,x.source_event_id,
                x.receipt_event_id,x.prior_tools,command.text AS operator_command,receipt.text AS receipt_text,
                source.text AS cited_statement
                FROM operator_concern_releases x JOIN room r ON r.thread_id=x.thread
                JOIN events command ON command.id=x.command_event_id AND command.role='operator'
                JOIN events receipt ON receipt.id=x.receipt_event_id AND receipt.role='system'
                LEFT JOIN events source ON source.id=x.source_event_id AND source.role='operator'
                WHERE x.thread=? AND ({selector}) ORDER BY x.receipt_event_id DESC LIMIT 16""", (thread, *parameters))
            results = []
            for row in rows:
                item = dict(row)
                # The worker emitted one of two fixed receipt headlines before appending
                # the free-form reason. This also distinguishes pre-migration releases.
                headline = item.pop("receipt_text").partition(".")[0]
                number = item["concern_id"]
                item["source"] = "authenticated_operator"
                item["operator_action"] = {
                    f"Mike retired context concern #{number}": "retired",
                    f"Mike confirmed the retirement of context concern #{number}": "confirmed_retirement",
                }.get(headline, "release_on_record")
                command = item.pop("operator_command").strip()
                reason = item["reason"]
                if (reason and len(command) > len(reason) and command.endswith(reason)
                        and command[-len(reason)-1].isspace()):
                    item["reason_source"] = "operator_text"
                else:
                    # A default reason, a citation and a confirmation can all carry
                    # host-written explanations. Keep the actual command distinct.
                    item["reason_source"] = "host_receipt"
                    item["operator_command"] = command[:MAX_STATEMENT]
                    if len(command) > MAX_STATEMENT:
                        item["operator_command_truncated"] = True
                tools = json.loads(item["prior_tools"])
                item["prior_tools"] = tools[:8]
                if len(tools) > 8:
                    item["prior_tools_omitted"] = len(tools) - 8
                statement = item.pop("cited_statement")
                if statement is not None:
                    item["cited_operator_statement"] = statement[:MAX_STATEMENT]
                    if len(statement) > MAX_STATEMENT:
                        item["cited_operator_statement_truncated"] = True
                results.append(item)
            return results

    def concern_releases(self, thread, tool):
        """Durable operator evidence for the former scope, independent of recent chat."""
        scopes = applicable_scopes(tool)
        marks = ",".join("?" for _ in scopes)
        return self._release_receipts(thread, f"""?='consult_opus' OR EXISTS
            (SELECT 1 FROM json_each(x.prior_tools) WHERE value IN ({marks}))""", (tool, *scopes))

    def releases_by_concern(self, thread, numbers):
        """Resolve named concerns even when their old scope differs from the proposed tool."""
        numbers = list(dict.fromkeys(numbers))[:4]
        if not numbers:
            return {}
        marks = ",".join("?" for _ in numbers)
        return {r["concern_id"]: r for r in self._release_receipts(thread, f"x.concern_id IN ({marks})", numbers)}

    def archive_receipt(self, thread):
        """The signed-in operator's original archive answer, when present in this room."""
        with self.store._connect() as db:
            row = db.execute("""SELECT q.id,q.question,q.answer,q.answered_at
                FROM operator_reviews q JOIN room r ON r.thread_id=q.thread
                WHERE q.id=? AND q.thread=? AND q.tool='read_heritage'
                    AND q.status='answered' AND q.final_decision='allow'""", (ARCHIVE_REVIEW_ID, thread)).fetchone()
            return dict(row) if row else None

    def retry_concern(self, review_id):
        """The operator explicitly reopens an unavailable concern final for one fresh answer."""
        if type(review_id) is not int or not 0 < review_id < 2**63:
            raise ValueError("Enter a positive review ID")
        with self.store._connect(write=True) as db:
            row = db.execute("""SELECT q.* FROM operator_reviews q JOIN room r ON r.thread_id=q.thread
                WHERE q.id=? AND q.tool='context_concern' AND q.status='answered'
                AND q.final_decision='unavailable'""", (review_id,)).fetchone()
            if row is None:
                raise ValueError("Only an unavailable final context review can be reopened")
            if self._released(db, row["thread"], row["scope"]):
                raise ValueError("The operator has already retired this context concern")
            call_id = f"host:retry-context-review:{review_id}"
            latest = db.execute("""SELECT id,call_id FROM operator_reviews
                WHERE thread=? AND scope=? ORDER BY id DESC LIMIT 1""", (row["thread"], row["scope"])).fetchone()
            if latest["call_id"] == call_id:
                return latest["id"]
            if latest["id"] != review_id:
                raise ValueError("A newer review exists for this concern")
            return db.execute("""INSERT INTO operator_reviews
                (thread,scope,tool,call_id,action,excerpt,question,source,asked_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (row["thread"], row["scope"], row["tool"], call_id,
                    row["action"], row["excerpt"], row["question"], row["source"],
                    self.store.clock().isoformat())).lastrowid

    def answer(self, review_id, text):
        """Save the operator's exact words and queue one wake in the same transaction."""
        _text(text, MAX_ANSWER, "operator answer")
        with self.store._connect(write=True) as db:
            row = db.execute("""SELECT q.* FROM operator_reviews q JOIN room r ON r.thread_id=q.thread
                WHERE q.id=?""", (review_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown operator question")
            if row["status"] == "answered":
                if row["answer"] != text:
                    raise ValueError("This question already has a different answer")
                return row["wake_id"]
            origin = "Opus asked" if row["source"] == "opus" else "The host asked about an earlier Opus denial"
            if row["tool"] == "context_concern":
                next_step = "The host will revisit the context concern using this answer. No new tool proposal is needed."
            else:
                next_step = "If the action is still appropriate, submit a fresh proposal for review."
            wake = (f"Operator review #{review_id}. {origin} a question and Mike answered directly in the "
                    f"private operator room. {next_step}\n" + json.dumps(
                        {"tool": row["tool"], "action": row["action"], "scope": row["scope"],
                         "question": row["question"], "operator_answer": text}, ensure_ascii=False))
            event_id = self.store._append(db, "system", wake)
            db.execute("INSERT INTO inbox(event_id) VALUES(?)", (event_id,))
            db.execute("""UPDATE operator_reviews SET status='answered',answer=?,answered_at=?,wake_id=?
                WHERE id=?""", (text, self.store.clock().isoformat(), event_id, review_id))
            return event_id

    def statements(self, thread, batch_id):
        """Human messages admitted by the host through this batch, with whole quotations only."""
        if batch_id is None:
            return []
        with self.store._connect() as db:
            rows = db.execute("""SELECT e.id,e.at,e.text FROM events e JOIN inbox i ON i.event_id=e.id
                WHERE e.role='operator' AND i.batch_id<=?
                    AND EXISTS(SELECT 1 FROM batches WHERE id=?)
                    AND EXISTS(SELECT 1 FROM room WHERE thread_id=?)
                ORDER BY e.id DESC LIMIT 3""", (batch_id, batch_id, thread)).fetchall()
        result = []
        for row in reversed(rows):
            statement = {"event_id": row["id"], "at": row["at"]}
            if len(row["text"]) <= MAX_STATEMENT:
                statement["text"] = row["text"]
            else:
                statement.update(omitted=True, length=len(row["text"]))
            result.append(statement)
        return result

    def claim_final(self, review_id, fingerprint):
        """Only the caller receiving 'claimed' may pay for the final decision."""
        _text(fingerprint, 256, "proposal fingerprint")
        with self.store._connect(write=True) as db:
            row = db.execute("""SELECT q.status,q.final_fingerprint,q.final_decision
                FROM operator_reviews q JOIN room r ON r.thread_id=q.thread WHERE q.id=?""",
                             (review_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown operator question")
            if row["status"] != "answered":
                raise ValueError("This question is awaiting the operator")
            if row["final_fingerprint"] is None:
                db.execute("UPDATE operator_reviews SET final_fingerprint=? WHERE id=?", (fingerprint, review_id))
                return "claimed"
            if row["final_fingerprint"] != fingerprint:
                return "used"
            return row["final_decision"] or "in_progress"

    def finish_final(self, review_id, fingerprint, decision, reason, *, tools=None):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Invalid final judgment")
        with self.store._connect(write=True) as db:
            row = db.execute("""SELECT thread,scope,tool,final_decision,final_reason,final_tools FROM operator_reviews
                WHERE id=? AND thread=(SELECT thread_id FROM room WHERE id=1) AND final_fingerprint=?""",
                             (review_id, fingerprint)).fetchone()
            if row is None:
                raise ValueError("No matching claim for this final judgment")
            concern = row["tool"] == "context_concern"
            if concern and row["final_decision"] == "retire" and self._released(db, row["thread"], row["scope"]):
                return
            allowed = ("retire", "watch", "unavailable") if concern else ("allow", "deny", "unavailable")
            if decision not in allowed:
                raise ValueError("Invalid final judgment for this review")
            encoded = None
            if decision in ("retire", "watch"):
                scope = tools if tools is not None else []
                validate_scope(decision, scope)
                encoded = json.dumps(sorted(scope))
            elif tools not in (None, []):
                raise ValueError("Unexpected tool scope for this final judgment")
            if (row["final_decision"] is not None and
                    (row["final_decision"], row["final_reason"], row["final_tools"]) != (decision, reason, encoded)):
                raise ValueError("No matching claim for this final judgment")
            if row["final_decision"] is None:
                db.execute("UPDATE operator_reviews SET final_decision=?,final_reason=?,final_tools=? WHERE id=?",
                           (decision, reason, encoded, review_id))
                label = {"allow": "Opus allowed the proposed action", "deny": "Opus denied the proposed action",
                         "retire": "Opus retired the context concern", "watch": "Opus kept the context concern open",
                         "unavailable": "The host could not confirm an Opus decision"}[decision]
                if decision == "watch":
                    label += " for " + ("all tools" if tools == ["*"] else ", ".join(sorted(tools)))
                notice = f"Review #{review_id}. {label}.\n{reason}"
                if concern and decision == "unavailable":
                    notice += f"\nMike may reopen the question from his operator terminal with lantern retry-concern {review_id}."
                self.store._append(db, "system", notice)

    def recover_finals(self):
        """The worker owns the exclusive room lock at startup. Never repeat an uncertain paid call."""
        with self.store._connect(write=True) as db:
            rows = db.execute("""SELECT id,tool FROM operator_reviews WHERE thread=(SELECT thread_id FROM room)
                AND final_fingerprint IS NOT NULL AND final_decision IS NULL""").fetchall()
            reason = "The worker restarted before recording the judgment. Mike can supply new information before another review."
            for row in rows:
                db.execute("UPDATE operator_reviews SET final_decision='unavailable',final_reason=? WHERE id=?",
                           (reason, row["id"]))
                notice = f"Review #{row['id']} remains held. {reason}"
                if row["tool"] == "context_concern":
                    notice += f"\nMike may reopen the question from his operator terminal with lantern retry-concern {row['id']}."
                self.store._append(db, "system", notice)

    def release_final(self, review_id, fingerprint):
        """Release a claim only when the caller knows no provider request was sent."""
        with self.store._connect(write=True) as db:
            updated = db.execute("""UPDATE operator_reviews SET final_fingerprint=NULL
                WHERE id=? AND thread=(SELECT thread_id FROM room WHERE id=1)
                    AND final_fingerprint=? AND final_decision IS NULL""", (review_id, fingerprint)).rowcount
            if not updated:
                row = db.execute("""SELECT thread,scope FROM operator_reviews WHERE id=? AND tool='context_concern'
                    AND thread=(SELECT thread_id FROM room WHERE id=1) AND final_fingerprint=?
                    AND final_decision='retire'""", (review_id, fingerprint)).fetchone()
                if row and self._released(db, row["thread"], row["scope"]):
                    return
                raise ValueError("No matching unreconciled claim")
