import json
import unittest
from unittest.mock import Mock, patch

from lantern import opus


def receipt(text, key="a" * 32):
    body = {"model": opus.OPUS_MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}]}
    return opus.AuditedReply(body, lambda _: None, request_id=key)


class ShadowObservation(unittest.TestCase):
    def test_exact_wire_evidence_and_independent_copy_of_binding_verdict(self):
        facts = {"operator_statements": [{"text": "You may retry."}],
                 "prior_exact_denial": "Held earlier.", "standing_grant": {"purpose": "Private copy."}}
        evidence = {"host_facts": facts, "operator_statements": facts["operator_statements"],
                    "prior_action_denial": facts["prior_exact_denial"], "tool": "workspace_read",
                    "arguments": {"path": "notes/decision.md", "host_facts": "untrusted text"},
                    "operator_question_available": True}
        vendor = Mock(return_value=receipt('<review decision="allow">Current grant applies.</review>'))
        events = []

        def observe(event):
            events.append(event)
            event["opus_result"]["decision"] = "deny"

        result = opus.review(vendor, evidence, operator_context="Private room.", observer=observe)
        vendor.assert_called_once()
        wire = vendor.call_args.args[1]
        event, = events
        snap = event["snapshot"]
        self.assertEqual((event["request_id"], event["status"], event["error"]), ("a" * 32, "ok", None))
        self.assertEqual(snap["case_text"], wire["messages"][0]["content"])
        self.assertEqual(json.loads(snap["case_text"])["host_facts"], facts)
        self.assertIn(snap["utc_date"], wire["system"])
        self.assertEqual(snap["operator_context"], "Private room.")
        self.assertTrue(snap["has_action"] and snap["can_ask"])
        self.assertEqual(snap["concern_ids"], [])
        self.assertNotIn("Current grant applies.", json.dumps(snap))
        self.assertEqual(result["decision"], "allow")

    def test_standalone_post_answer_and_batched_watches(self):
        c2 = {"id": 2, "scope_tools": ["workspace_write"]}
        c7 = {"id": 7, "scope_tools": ["workspace_write"]}
        first = '<concern id="2" decision="close_resolved">Grant verified.</concern>'
        second = '<concern id="7" decision="keep_open" tools="workspace_write">Hold external publishing.</concern>'
        question = '<review decision="ask_operator">Is that publication intended?</review>'
        for evidence, xml, expected in [
            ({"concern_review": c2, "concern_question_available": True}, first, (False, False, True, [2])),
            ({"concern_review": c2, "concern_attestation": {"answer": "Verified my copy."},
              "concern_question_available": True}, first, (False, True, False, [2])),
            ({"tool": "workspace_write", "concern_reviews": [c2, c7], "operator_question_available": True},
             "<ruling>" + question + second + first + "</ruling>", (True, False, False, [2, 7])),
        ]:
            with self.subTest(evidence=evidence):
                events = []
                result = opus.review(Mock(return_value=receipt(xml)), evidence, observer=events.append)
                event, = events
                snap = event["snapshot"]
                self.assertEqual((snap["has_action"], snap["final"], snap["can_ask_concern"],
                                  snap["concern_ids"]), expected)
                self.assertEqual(event["opus_result"], result)

    def test_search_is_one_exclusion_even_if_the_followup_request_fails(self):
        first = receipt('<search source="wikipedia">chess history</search>')
        reply = receipt('<review decision="deny">Scope remains unclear.</review>', "b" * 32)
        for continuation in [reply, OSError("provider outage")]:
            with self.subTest(continuation=type(continuation)):
                events = []
                vendor = Mock(side_effect=[first, continuation])
                with patch.object(opus, "public_search", return_value=[]):
                    if isinstance(continuation, OSError):
                        with self.assertRaises(OSError):
                            opus.review(vendor, {"tool": "web_search"}, observer=events.append)
                    else:
                        self.assertEqual(opus.review(vendor, {"tool": "web_search"}, observer=events.append)["decision"], "deny")
                event, = events
                self.assertEqual((event["status"], event["request_id"]), ("excluded_research", "a" * 32))
                self.assertNotIn("untrusted_research_result", event["snapshot"]["case_text"])

    def test_malformed_opus_result_is_counted_and_stays_a_hold(self):
        events = []
        with self.assertRaises(opus.OpusResponseError) as raised:
            opus.review(Mock(return_value=receipt("no verdict")), {"tool": "workspace_read"}, observer=events.append)
        event, = events
        self.assertEqual((event["status"], event["error"], event["opus_result"]),
                         ("invalid_opus", raised.exception.code, None))

    def test_failed_observer_cannot_change_binding_decision_or_exception(self):
        observer = Mock(side_effect=OSError("private file information"))
        with self.assertLogs("lantern.opus", level="WARNING") as logs:
            self.assertEqual(opus.review(Mock(return_value=receipt('<review decision="deny">Held.</review>')),
                                         {}, observer=observer)["decision"], "deny")
            with self.assertRaises(opus.OpusResponseError):
                opus.review(Mock(return_value=receipt("bad format")), {}, observer=observer)
        self.assertNotIn("private file information", str(logs.output))
        self.assertEqual(observer.call_count, 2)

    def test_unaudited_receipts_and_vendor_failures_skip_shadow(self):
        observer = Mock()
        for response in (dict(receipt('<review decision="allow">Ok.</review>')),
                         receipt('<review decision="allow">Ok.</review>', None)):
            opus.review(Mock(return_value=response), {}, observer=observer)
        with self.assertRaises(OSError):
            opus.review(Mock(side_effect=OSError("offline")), {}, observer=observer)
        observer.assert_not_called()
