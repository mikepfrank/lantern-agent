"""Tool receipts preserve the reviewer's visible words and the host's separate outcome."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from lantern.opus import OpusResponseError
from lantern.supervision import Supervisor


CLEAR = {"escalate": False, "evidence": "", "summary": "Routine request"}
FLAG = {"escalate": True, "evidence": "Needs review", "summary": "Check this request"}


class ReviewDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.screen, self.review, self.consult = Mock(), Mock(), Mock()
        self.execute = Mock(return_value={"saved": True})
        self.screen.screen.return_value = CLEAR
        self.gate = self.session("own")

    def session(self, thread):
        gate = Supervisor(self.state, self.screen, self.review, self.consult, sample_every=1, batch_id=19)
        gate.bind(thread)
        gate.observe({"input": "Test"})
        return gate

    def call(self, cid, gate=None):
        return (gate or self.gate).dispatch(cid, "chess_analyze", {"rationale": "Analyze."}, self.execute)

    def test_allow_deny_unreadable_and_resume(self):
        self.screen.screen.return_value = FLAG
        for decision in ("allow", "deny"):
            text = f"Before\n<review decision='{decision}'>Reason</review>\nAfter"
            self.review.return_value = {"decision": decision, "reason": "Reason", "response_text": text}
            result = self.call(decision)
            self.assertEqual(result["opus_review_text"], text)
            self.assertEqual("error" in result, decision == "deny")
            self.assertEqual(self.call(decision), result)
        error = OpusResponseError("malformed_xml")
        error.response_text = "Unfinished <review>"
        self.review.side_effect = error
        held = self.call("incomplete")
        self.assertIn("unreadable", held["error"])
        self.assertEqual(held["opus_review_text"], error.response_text)
        self.screen.screen.return_value = CLEAR
        self.assertEqual(self.call("incomplete", self.session("own")), held)
        self.execute.assert_called_once()

    def test_consultation_keeps_separate_action_review_and_advice(self):
        self.screen.screen.return_value = FLAG
        self.review.return_value = {"decision": "allow", "reason": "Fine to ask.", "response_text": "Ruling"}
        self.consult.return_value = {"advice": "Guidance", "research": [], "available_research": ["wikipedia", "crossref"],
                                     "response_text": "Before <advice>Guidance</advice> After"}
        args = {"question": "Is this appropriate?", "rationale": "Request advice."}
        result = self.gate.dispatch("ask", "consult_opus", args, self.execute)
        self.assertEqual(result["opus_review_text"], "Ruling")
        self.assertEqual(result["opus_advice_text"], self.consult.return_value["response_text"])
        self.assertEqual(result["advice"], "Guidance")
        error = OpusResponseError("malformed_xml")
        error.response_text = "Advice with no decision."
        self.consult.side_effect = error
        result = self.gate.dispatch("unreadable", "consult_opus", args, self.execute)
        self.assertIn("unreadable", result["error"])
        self.assertEqual(result["opus_advice_text"], error.response_text)
        self.assertEqual(result["opus_review_text"], "Ruling")

    def test_context_feedback_is_scoped_ordered_and_durable(self):
        def close(case):
            cid = case["concern_review"]["id"]
            return {"concern": {"id": cid, "decision": "retire", "reason": "Resolved", "tools": []},
                    "response_text": f"Whole context reply {cid}"}

        self.review.side_effect = close
        self.screen.screen.return_value = FLAG
        for _ in range(2):
            self.gate.observe({"input": "Sampled context"})
        self.screen.screen.return_value = CLEAR
        self.assertNotIn("opus_context_review", self.call("other", self.session("other")))
        resumed = self.session("own")
        for cid in (1, 2):
            result = self.call(str(cid), resumed)
            self.assertEqual(result["opus_context_review"], {"concern_id": cid, "turn": 19,
                             "parsed_decision": "retire", "response_text": f"Whole context reply {cid}"})
            self.assertEqual(self.call(str(cid), resumed), result)
        self.assertNotIn("opus_context_review", self.call("empty", resumed))
