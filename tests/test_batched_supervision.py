"""Multiple matching watches share one decision on the exact proposal."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from lantern import concerns, opus
from lantern.chat_store import ChatStore
from lantern.operator_review import ReviewRoom
from lantern.supervision import Supervisor, database


class BatchedSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.thread = "review-batch-test"
        self.store = ChatStore(self.state)
        self.store.submit("Review this private proposal.")
        self.batch = self.store.claim()
        self.store.bind_thread(self.batch.id, self.thread)
        self.room = ReviewRoom(self.store)
        self.execute = Mock(return_value={"done": True})
        self.luna, self.review = Mock(), Mock()
        self.matches = True
        self.luna.screen.side_effect = self.screen
        self.start()

    def start(self):
        self.gate = Supervisor(self.state, self.luna, self.review, Mock(), operator_room=self.room,
                               batch_id=self.batch.id, tool_names=["workspace_write", "message_operator"])
        self.gate.bind(self.thread)
        self.gate.observe({"input": "Private review fixture."})

    def screen(self, packet):
        answer = {"escalate": False, "evidence": "", "summary": "Ordinary private work."}
        if "scope_checks" in packet:
            answer["applicable_ids"] = [f["id"] for f in packet["scope_checks"]] if self.matches else []
        return answer

    def add_watch(self, tools):
        with database(self.gate.path) as db:
            n = concerns.create(db, self.thread, 1, "Earlier issue with a private report.", "Watch its destination.")
            concerns.record(db, self.thread, n, 0, "watch", "Follow actual attempts to share that report.", tools)
        return n

    def saved(self, number):
        with database(self.gate.path) as db:
            return concerns.get(db, self.thread, number)

    def write(self, call="write"):
        return self.gate.dispatch(call, "workspace_write", {"path": "notes/review.md", "text": "Private note.",
                                  "rationale": "Record the observed outcome."}, self.execute)

    def verdict(self, case, action="allow", disposition="watch"):
        return {"decision": action, "reason": "Is this external recipient within scope?" if action == "ask_operator"
                else "Decision on the one proposal.", "concerns": [
                    {"id": c["id"], "decision": disposition, "tools": [] if disposition == "retire" else c["tools"],
                     "reason": "Decision recorded for this concern."} for c in case["concern_reviews"]]}

    def use_wire_reply(self, action="allow", omit_first_scope=False):
        def vendor(name, payload, *, purpose):
            self.assertEqual((name, purpose), ("opus", "escalation"))
            findings = json.loads(payload["messages"][0]["content"])["case"]["concern_reviews"]
            children = []
            for index, item in enumerate(findings):
                scope = f' tools="{" ".join(item["tools"])}"'
                if index == 0 and omit_first_scope:
                    scope = ""
                children.append(f'<concern id="{item["id"]}" decision="keep_open"{scope}>'
                                'Watch the actual destination.</concern>')
            decision = "Who may receive this report?" if action == "ask_operator" else "Keep this work private."
            xml = f'<ruling><review decision="{action}">{decision}</review>{"".join(children)}</ruling>'
            return {"model": opus.OPUS_MODEL, "stop_reason": "end_turn", "content": [{"type": "text", "text": xml}]}
        self.review.side_effect = lambda case: opus.review(vendor, case)

    def test_four_write_and_six_message_watches_make_two_reviews_and_no_popups(self):
        writes = [self.add_watch(["workspace_write", "message_operator"]) for _ in range(4)]
        messages = writes + [self.add_watch(["message_operator"]) for _ in range(2)]
        self.use_wire_reply()
        self.assertEqual(self.write(), {"done": True})
        result = self.gate.dispatch("notify", "message_operator",
                                    {"text": "The private note was saved.", "rationale": "Update Mike."}, self.execute)
        self.assertEqual(result, {"done": True})
        groups = [[f["id"] for f in call.args[0]["concern_reviews"]] for call in self.review.call_args_list]
        self.assertEqual(groups, [writes, messages])
        self.assertEqual(self.room.pending(), [])
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual([self.saved(n)["version"] for n in messages], [3, 3, 3, 3, 2, 2])

    def test_archived_style_action_question_from_invalid_batch_does_not_apply_any_dispositions(self):
        ids = [self.add_watch(["workspace_write"]) for _ in range(3)]
        self.use_wire_reply("ask_operator", omit_first_scope=True)
        result = self.write()
        popup, = self.room.pending()
        self.assertEqual(popup["question"], "Who may receive this report?")
        self.assertIn("invalid_concern_scope", popup["excerpt"])
        self.assertEqual(self.write("waiting")["review_id"], result["review_id"])
        self.assertEqual([self.saved(n)["version"] for n in ids], [1, 1, 1])
        self.review.assert_called_once()
        self.execute.assert_not_called()

    def test_incomplete_verdict_holds_all_receipts_and_exact_action_across_restart(self):
        ids = [self.add_watch(["workspace_write"]) for _ in range(2)]
        self.review.return_value = {"decision": "allow", "reason": "Incomplete judgment.",
                                    "concerns": [{"id": ids[0], "decision": "retire", "tools": [], "reason": "Done."}]}
        held = self.write()
        self.assertIn("invalid_concern_ruling", held["error"])
        self.assertEqual([self.saved(n)["version"] for n in ids], [1, 1])
        self.assertEqual(self.room.pending(), [])
        self.matches = False
        self.start()
        self.assertEqual(self.write(), held)
        self.review.return_value = {"decision": "deny", "reason": "The disclosure needs review."}
        self.assertIn("denied", self.write("fresh")["error"])
        self.assertEqual(self.review.call_count, 2)
        self.assertIn("prior_incomplete_review", self.review.call_args.args[0])
        self.assertIn("denied", self.write("try-again")["error"])
        self.assertIn("prior_action_denial", self.review.call_args.args[0])
        self.execute.assert_not_called()

    def test_concurrent_retirement_rolls_back_other_rulings_and_keeps_action_held(self):
        first, second = [self.add_watch(["workspace_write"]) for _ in range(2)]
        def supersede(case):
            with database(self.gate.path) as db:
                concerns.record(db, self.thread, second, 1, "retire", "Independent resolution.", [])
            return self.verdict(case, disposition="retire")
        self.review.side_effect = supersede
        self.assertIn("changed during review", self.write()["error"])
        self.assertEqual((self.saved(first)["status"], self.saved(first)["version"]), ("watch", 1))
        self.assertEqual(self.saved(second)["reason"], "Independent resolution.")
        self.execute.assert_not_called()
        self.assertEqual(self.room.pending(), [])

    def test_one_question_preserves_all_watches_and_answered_final_requires_valid_group(self):
        ids = [self.add_watch(["workspace_write"]) for _ in range(2)]
        self.review.side_effect = lambda case: self.verdict(case, "ask_operator")
        held = self.write()
        popup, = self.room.pending()
        self.assertEqual(held["review_id"], popup["id"])
        self.assertEqual(self.write("waiting")["review_id"], popup["id"])
        self.review.assert_called_once()
        self.room.answer(popup["id"], "Only the original private destination.")
        self.store.complete(self.batch.id, "Waiting for the answer.")
        self.batch = self.store.claim()
        self.start()
        self.assertEqual(self.write(), held)
        self.review.side_effect = lambda case: self.verdict(case, "deny", "retire")
        self.assertIn("denied", self.write("final")["error"])
        self.assertEqual(self.review.call_count, 2)
        case = self.review.call_args.args[0]
        self.assertEqual(case["operator_attestation"]["answer"], "Only the original private destination.")
        self.assertEqual([c["id"] for c in case["concern_reviews"]], ids)
        self.assertEqual(self.room.case(self.thread, "workspace_write:notes/review.md")["final_decision"], "deny")
        self.assertTrue(all(self.saved(n)["status"] == "retired" for n in ids))
        self.assertEqual(self.room.pending(), [])
        self.execute.assert_not_called()
