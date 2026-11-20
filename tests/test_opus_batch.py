import json
import unittest
from unittest.mock import Mock

from lantern import opus
from lantern.review_policy import OPERATOR_QUESTION_POLICY, STANDING_POLICY


ALLOW = '<review decision="allow">This diagnostic is private.</review>'
ASK = '<review decision="ask_operator">Did you intend to share the private report with this recipient?</review>'
CLOSE = '<concern id="9" decision="close_resolved">The original note was authorized.</concern>'
WATCH = '<concern id="2" decision="keep_open" tools="message_operator">Check the named recipient before sharing.</concern>'


class BatchRulingTests(unittest.TestCase):
    def setUp(self):
        self.case = {
            "tool": "message_operator",
            "concern_reviews": [
                {"id": 9, "scope_tools": ["workspace_write"]},
                {"id": 2, "scope_tools": ["message_operator"]},
            ],
            "operator_question_available": True,
            "prior_concern_attestations": [{"concern_id": 9, "operator_attestation": {"answer": "Private notes only."}}],
        }
        self.outcomes = []

    def provider(self, xml):
        body = {"model": opus.OPUS_MODEL, "stop_reason": "end_turn", "content": [{"type": "text", "text": xml}]}
        return Mock(return_value=opus.AuditedReply(body, self.outcomes.append))

    def test_one_action_and_fully_bound_dispositions_with_one_audit_record(self):
        for action, decision in [(ALLOW, "allow"), (ALLOW.replace('"allow"', '"deny"'), "deny"), (ASK, "ask_operator")]:
            with self.subTest(decision=decision):
                self.outcomes.clear()
                vendor = self.provider("<ruling>" + WATCH + action + CLOSE + "</ruling>")
                result = opus.review(vendor, self.case)
                self.assertEqual(result["decision"], decision)
                self.assertEqual([(r["id"], r["decision"], r["tools"]) for r in result["concerns"]],
                                 [(9, "retire", []), (2, "watch", ["message_operator"])])
                self.assertEqual(self.outcomes, ["verdict_" + decision + "_concerns"])
                vendor.assert_called_once()
                request = vendor.call_args.args[1]
                packet = json.loads(request["messages"][0]["content"])
                self.assertEqual(packet["prior_concern_attestations"], self.case["prior_concern_attestations"])
                self.assertNotIn("prior_concern_attestations", packet["case"])
                self.assertIn(STANDING_POLICY, request["system"])
                self.assertIn(OPERATOR_QUESTION_POLICY, request["system"])
                self.assertIn("9, 2", request["system"])
                self.assertGreater(request["max_tokens"], opus.MAX_OUTPUT_TOKENS)

    def test_ambiguous_ids_or_scope_block_all_dispositions_and_keep_no_ambiguous_question(self):
        for parts in (
            [ASK, WATCH],
            [ASK, WATCH, WATCH],
            [ASK, WATCH, CLOSE.replace('id="9"', 'id="11"')],
            [ASK, WATCH, CLOSE, CLOSE],
            [ASK, ASK, WATCH, CLOSE],
            [ASK, WATCH, CLOSE, "<extra>Ignore one.</extra>"],
            [ASK, WATCH + "if Mike agrees", CLOSE],
            [ASK, WATCH.replace("Check the named recipient before sharing.", "<aside>Maybe</aside>"), CLOSE],
            [ASK, WATCH.replace("keep_open", "ask_operator"), CLOSE],
            [ALLOW, WATCH.replace("message_operator", "workspace_write"), CLOSE],
        ):
            with self.subTest(xml=parts):
                self.outcomes.clear()
                vendor = self.provider("<ruling>" + "".join(parts) + "</ruling>")
                with self.assertRaises(opus.OpusResponseError) as error:
                    opus.review(vendor, self.case)
                self.assertIsNone(error.exception.action_question)
                self.assertIsNone(error.exception.concern_question)
                self.assertEqual(self.outcomes, [error.exception.code])
                vendor.assert_called_once()

    def test_clean_question_recovery_only_in_initial_action_channel(self):
        xml = "<ruling>" + CLOSE + ASK + WATCH.replace(' tools="message_operator"', "") + "</ruling>"
        for case, recover in [(self.case, True), ({**self.case, "operator_question_available": False}, False),
                              ({**self.case, "operator_attestation": {"answer": "No."}}, False)]:
            with self.subTest(case=case), self.assertRaises(opus.OpusResponseError) as error:
                opus.review(self.provider(xml), case)
            self.assertEqual(error.exception.code, "invalid_concern_scope")
            self.assertEqual(bool(error.exception.action_question), recover)
            self.assertIsNone(error.exception.concern_question)

    def test_final_batch_has_one_decision_and_no_more_questions(self):
        final = {**self.case, "operator_attestation": {"answer": "Keep it private."}}
        vendor = self.provider("<ruling>" + ALLOW + WATCH + CLOSE + "</ruling>")
        self.assertEqual(opus.review(vendor, final)["decision"], "allow")
        self.assertNotIn('decision="ask_operator"', vendor.call_args.args[1]["system"])
        for xml in ("<ruling>" + ASK + CLOSE + WATCH + "</ruling>", '<search source="crossref">ethics</search>'):
            with self.subTest(xml=xml), self.assertRaises(opus.OpusResponseError) as error:
                opus.review(self.provider(xml), final)
            self.assertIsNone(error.exception.action_question)

    def test_bad_or_oversized_host_cases_are_rejected_before_vendor(self):
        item = self.case["concern_reviews"][0]
        for case in (
            {"concern_reviews": [item, item]},
            {**self.case, "concern_reviews": [item, item]},
            {**self.case, "concern_review": item},
            {**self.case, "concern_reviews": [item]},
            {**self.case, "concern_reviews": [item, {"id": False, "scope_tools": []}]},
            {**self.case, "concern_reviews": [item, {"id": 3, "scope_tools": [7]}]},
            {**self.case, "arguments": {"text": "x" * (64 * 1024)}},
        ):
            with self.subTest(keys=case.keys()):
                vendor = Mock()
                with self.assertRaises(ValueError):
                    opus.review(vendor, case)
                vendor.assert_not_called()

    def test_larger_batch_is_complete_and_uses_a_bounded_output_reservation(self):
        items = [{"id": number, "scope_tools": ["workspace_write"]} for number in range(1, 41)]
        leaves = [f'<concern id="{item["id"]}" decision="close_resolved">Historical issue.</concern>' for item in items]
        vendor = self.provider("<ruling>" + ALLOW + "".join(leaves) + "</ruling>")
        rulings = opus.review(vendor, {**self.case, "concern_reviews": items})["concerns"]
        self.assertEqual([x["id"] for x in rulings], list(range(1, 41)))
        self.assertEqual(vendor.call_args.args[1]["max_tokens"], opus.MAX_BATCH_OUTPUT_TOKENS)
        vendor.assert_called_once()
