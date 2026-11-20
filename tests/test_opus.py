from datetime import datetime, timezone
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from lantern import opus


def response(text):
    return {"model": opus.OPUS_MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}]}


class OpusTests(unittest.TestCase):
    def test_channel_context_reaches_review_and_consult_from_the_host_only(self):
        channel = "Mike is the sole human on this private CLI."
        claim = "A claim from the conversation."
        for mode, text in ((opus.review, '<review decision="deny">Hold this action.</review>'),
                           (opus.consult, '<advice>Ask first.</advice>')):
            for host_context in ("", channel):
                with self.subTest(mode=mode.__name__, host_context=host_context):
                    vendor = Mock(return_value=response(text))
                    mode(vendor, {"operator_context": claim}, operator_context=host_context)
                    body = vendor.call_args.args[1]
                    self.assertEqual(channel in body["system"], bool(host_context))
                    self.assertNotIn(claim, body["system"])
                    self.assertIn(claim, body["messages"][0]["content"])

    def test_legacy_reviewer_receives_context_and_can_deny(self):
        case = {"tool": "chess_analyze", "arguments": {"fen": "test position"},
                "rationale": "Find a mate", "evidence": "fixture concern", "summary": "fixture summary"}
        vendor = Mock(return_value=response('<review decision="deny">Verify the source first.</review>'))
        self.assertEqual(opus.review(vendor, case), {"decision": "deny", "reason": "Verify the source first."})
        (provider, body), kwargs = vendor.call_args
        self.assertEqual((provider, kwargs["purpose"]), ("opus", "escalation"))
        self.assertEqual(body["model"], "claude-3-opus-20240229")
        self.assertEqual(body["max_tokens"], opus.MAX_OUTPUT_TOKENS)
        self.assertIn("March 4, 2024", body["system"])
        self.assertIn(datetime.now(timezone.utc).date().isoformat(), body["system"])
        self.assertIn("Michael P. Frank", body["system"])
        self.assertIn("Begin your response with the opening XML tag", body["system"])
        self.assertEqual(json.loads(body["messages"][0]["content"]), {"case": case})

    def test_optional_research_is_separate_from_advisory_authority(self):
        vendor = Mock(side_effect=[
            response('<search source="crossref">computer ethics</search>'),
            response("<advice>Ask permission before using private records.</advice>"),
        ])
        with patch("lantern.opus.public_search", return_value=[{"title": "untrusted hit"}]) as search:
            answer = opus.consult(vendor, {"arguments": {"question": "How should I proceed?"}})
        self.assertEqual(answer["advice"], "Ask permission before using private records.")
        self.assertEqual(answer["research"], [{"source": "crossref", "status": "ok", "hits": 1}])
        search.assert_called_once_with("crossref", "computer ethics")
        self.assertEqual(len(vendor.call_args_list[0].args[1]["messages"]), 1)
        self.assertEqual([c.kwargs["purpose"] for c in vendor.call_args_list], ["consult", "consult"])
        msg = json.loads(vendor.call_args.args[1]["messages"][-1]["content"])
        self.assertEqual(msg["untrusted_research_result"]["hits"], [{"title": "untrusted hit"}])
        self.assertEqual(msg["searches_remaining"], 1)
        with self.assertRaises(ValueError):
            opus.review(Mock(return_value=response("<advice>Looks fine.</advice>")), {})

    def test_question_holds_a_disputed_action_and_separates_operator_words(self):
        statements = [{"text": "I approved a private record."}]
        proposal = {"tool": "workspace_write", "arguments": {"operator_attestation": "forged answer"}}
        case = {**proposal, "operator_question_available": True, "operator_statements": statements}
        text = "Does this approval cover the proposed private file?"
        observed = []
        vendor = Mock(return_value=opus.AuditedReply(
            response(f'<review decision="ask_operator">{text}</review>'), observed.append))
        self.assertEqual(opus.review(vendor, case), {"decision": "ask_operator", "reason": text})
        vendor.assert_called_once()
        self.assertEqual(observed, ["ask_operator"])
        body = vendor.call_args.args[1]
        self.assertEqual(json.loads(body["messages"][0]["content"]),
                         {"case": proposal, "operator_statements": statements})
        self.assertNotIn("forged answer", body["system"])
        self.assertIn('decision="ask_operator"', body["system"])

    def test_question_is_unavailable_outside_an_enabled_review(self):
        ask = response('<review decision="ask_operator">Did you approve this?</review>')
        for mode, case in (
            (opus.review, {"tool": "workspace_write", "arguments": {"operator_question_available": True}}),
            (opus.review, {"tool": "consult_opus", "operator_question_available": True}),
            (opus.review, {"tool": "unknown", "host_status": "unregistered", "operator_question_available": True}),
            (opus.consult, {"operator_question_available": True}),
        ):
            vendor = Mock(return_value=ask)
            with self.subTest(mode=mode, case=case), self.assertRaises(opus.OpusResponseError):
                mode(vendor, case)
            vendor.assert_called_once()
        too_long = "a" * (opus.OPERATOR_QUESTION_LIMIT + 1)
        with self.assertRaises(opus.OpusResponseError):
            opus.review(Mock(return_value=response(f'<review decision="ask_operator">{too_long}</review>')),
                        {"tool": "workspace_write", "operator_question_available": True})

    def test_direct_answer_gets_one_final_call_without_lookup_or_repeat_question(self):
        attestation = {"question": "Is this file in scope?", "answer": "Yes, for this private note."}
        case = {"tool": "workspace_write", "operator_question_available": True,
                "operator_attestation": attestation}
        vendor = Mock(return_value=response('<review decision="allow">The scope matches.</review>'))
        self.assertEqual(opus.review(vendor, case)["decision"], "allow")
        vendor.assert_called_once()
        body = vendor.call_args.args[1]
        self.assertEqual(json.loads(body["messages"][0]["content"]),
                         {"case": {"tool": "workspace_write"}, "operator_attestation": attestation})
        self.assertNotIn(attestation["answer"], body["system"])
        self.assertNotIn('decision="ask_operator"', body["system"])
        for text in ('<review decision="ask_operator">Could you clarify further?</review>',
                     '<search source="wikipedia">ethics</search>'):
            vendor = Mock(return_value=response(text))
            with self.subTest(text=text), patch("lantern.opus.public_search") as search:
                with self.assertRaises(opus.OpusResponseError):
                    opus.review(vendor, case)
                vendor.assert_called_once()
                search.assert_not_called()

    def test_review_accepts_neutral_headings_without_another_call(self):
        for tool, heading, decision in (
            ("workspace_write", "Here is my review of the proposed workspace_write action:", "allow"),
            ("read_heritage", "Here is my review decision regarding the proposed read_heritage tool use:", "allow"),
            ("workspace_write", "Here is my review decision regarding the proposed workspace write:", "allow"),
            ("workspace_write", "Here’s my review:", "deny"),
            ("read_heritage", "My review decision:", "ask_operator"),
        ):
            with self.subTest(tool=tool, decision=decision):
                original = f'{heading}\n\n<review decision="{decision}">First line.\n\nSecond &amp; last.</review>'
                outcomes = []
                reply = opus.AuditedReply(response(original), outcomes.append)
                vendor = Mock(return_value=reply)
                result = opus.review(vendor, {"tool": tool, "operator_question_available": True})
                self.assertEqual(result, {"decision": decision, "reason": "First line.\n\nSecond & last."})
                self.assertEqual(outcomes, ["ask_operator" if decision == "ask_operator" else "verdict_" + decision])
                self.assertEqual(reply["content"][0]["text"], original)
                vendor.assert_called_once()

    def test_standalone_and_combined_concern_rulings(self):
        concern = {"id": 42, "evidence": "Earlier held note", "scope_tools": ["workspace_write", "harness_logs"]}
        retire = '<concern id="42" decision="close_resolved">Mike clarified the earlier note.</concern>'
        watch = '<concern decision="keep_open" id="42" tools="workspace_write harness_logs">Check new copies.</concern>'
        action = '<review decision="allow">Chess analysis can proceed.</review>'
        for tool, text, disposition, tools, audit in (
            (None, retire, "retire", [], "concern_retire"),
            (None, watch, "watch", ["workspace_write", "harness_logs"], "concern_watch"),
            ("chess_analyze", action + watch, "watch", ["workspace_write", "harness_logs"], "verdict_allow_concern_watch"),
            ("chess_analyze", retire + action, "retire", [], "verdict_allow_concern_retire"),
        ):
            with self.subTest(tool=tool, disposition=disposition):
                case = {"concern_review": concern}
                if tool:
                    case["tool"] = tool
                outcomes = []
                xml = "<ruling>" + text + "</ruling>" if tool else text
                raw = response(xml)
                vendor = Mock(return_value=opus.AuditedReply(raw, outcomes.append))
                result = opus.review(vendor, case)
                self.assertEqual(result["concern"]["id"], 42)
                self.assertEqual(result["concern"]["decision"], disposition)
                self.assertEqual(result["concern"]["tools"], tools)
                self.assertEqual(result.get("decision"), "allow" if tool else None)
                self.assertEqual(outcomes, [audit])
                vendor.assert_called_once()
                self.assertEqual(vendor.call_args.kwargs["purpose"], "escalation" if tool else "context_review")
                instructions = vendor.call_args.args[1]["system"]
                self.assertIn('<concern id="42" decision="close_resolved"', instructions)
                self.assertIn("removes its extra checks on future actions", instructions)
                self.assertIn('<concern id="42" decision="keep_open" tools=', instructions)
                self.assertIn("the risk persists or resolution is uncertain", instructions)
                self.assertEqual(raw["content"][0]["text"], xml)
                self.assertIn("inside its concern and review children" if tool else "single concern element as the XML root",
                              instructions)

    def test_concern_question_uses_a_host_channel_and_final_answer(self):
        concern = {"id": 7, "scope_tools": ["workspace_write"]}
        case = {"concern_review": concern, "concern_question_available": True}
        ask = '<concern id="7" decision="ask_operator" tools="workspace_write">Does this cover reports?</concern>'
        watch = '<concern id="7" decision="keep_open" tools="*">All work paused until clarification.</concern>'
        answer = {"answer": "Pause work on this project for now."}
        self.assertEqual(opus.review(Mock(return_value=response(ask)), case)["concern"]["decision"], "ask_operator")
        vendor = Mock(return_value=response(watch))
        result = opus.review(vendor, {**case, "concern_attestation": answer})
        self.assertEqual(result["concern"]["tools"], ["*"])
        body = vendor.call_args.args[1]
        packet = json.loads(body["messages"][0]["content"])
        self.assertEqual(packet, {"case": {"concern_review": concern}, "concern_attestation": answer})
        self.assertNotIn("ask_operator", body["system"])
        prior = {**case, "tool": "workspace_write", "operator_question_available": True,
                 "prior_concern_attestation": answer}
        question = '<review decision="ask_operator">Does the earlier answer cover this new file?</review>'
        vendor = Mock(return_value=response('<ruling>' + watch + question + '</ruling>'))
        self.assertEqual(opus.review(vendor, prior)["decision"], "ask_operator")
        packet = json.loads(vendor.call_args.args[1]["messages"][0]["content"])
        self.assertEqual(packet["prior_concern_attestation"], answer)
        self.assertNotIn("prior_concern_attestation", packet["case"])
        for restricted, text in (
            ({"concern_review": {**concern, "concern_question_available": True}}, ask),
            ({**case, "concern_attestation": answer}, ask),
            ({**case, "concern_attestation": answer}, '<search source="crossref">ethics</search>'),
            ({"concern_review": concern, "tool": "workspace_write", "operator_question_available": True},
             '<ruling>' + ask + '<review decision="allow">Fine.</review></ruling>'),
            ({**case, "tool": "workspace_write"},
             '<ruling>' + watch + '<review decision="ask_operator">Is this covered?</review></ruling>'),
        ):
            with self.subTest(restricted=restricted, text=text), patch("lantern.opus.public_search") as search:
                vendor = Mock(return_value=response(text))
                with self.assertRaises(opus.OpusResponseError):
                    opus.review(vendor, restricted)
                vendor.assert_called_once()
                search.assert_not_called()

    def test_incomplete_or_ambiguous_concern_judgment_cannot_authorize(self):
        case = {"concern_review": {"id": 3, "scope_tools": ["workspace_write"]}, "tool": "workspace_write"}
        allow = '<review decision="allow">Permit this note.</review>'
        watch = '<concern id="3" decision="keep_open" tools="workspace_write">Review new copies.</concern>'
        for body in (
            allow, '<ruling>' + allow + '</ruling>', '<ruling>' + watch + '</ruling>',
            '<ruling>' + allow + allow + watch + '</ruling>', '<ruling>' + allow + watch + watch + '</ruling>',
            '<ruling>' + allow + ' only conditionally' + watch + '</ruling>',
            *('<ruling>' + allow + invalid + '</ruling>' for invalid in (
                watch.replace('id="3"', 'id="4"'), watch.replace('keep_open', 'resolved'),
                watch.replace('keep_open', 'watch'),
                '<concern id="3" decision="retire">The case can be closed.</concern>',
                watch.replace('keep_open', 'close_resolved'), watch.replace('tools="workspace_write"', ''),
                watch.replace('workspace_write', 'workspace_read'), watch.replace('workspace_write', '* workspace_write'),
                watch.replace('workspace_write', 'workspace_write workspace_write'), watch.replace('Review new copies.', ''),
                watch.replace('Review new copies.', '<review decision="allow">All.</review>'),
            )),
            'Only subject to new permission: <ruling>' + allow + watch + '</ruling>',
        ):
            with self.subTest(body=body):
                outcomes = []
                vendor = Mock(return_value=opus.AuditedReply(response(body), outcomes.append))
                with self.assertRaises(opus.OpusResponseError) as error:
                    opus.review(vendor, case)
                vendor.assert_called_once()
                self.assertEqual(outcomes, [error.exception.code])

    def test_combined_verdict_allows_one_operator_question(self):
        case = {"concern_review": {"id": 9, "scope_tools": ["workspace_write"]}, "tool": "workspace_write",
                "operator_question_available": True, "concern_question_available": True}
        for action, concern in (("deny", "ask_operator"), ("ask_operator", "keep_open"),
                                ("ask_operator", "ask_operator")):
            with self.subTest(action=action, concern=concern):
                xml = (f'<ruling><review decision="{action}">Can the note be saved?</review>'
                       f'<concern id="9" decision="{concern}" tools="workspace_write">'
                       'Which notes are in scope?</concern></ruling>')
                audit = []
                vendor = Mock(return_value=opus.AuditedReply(response(xml), audit.append))
                if action == concern == "ask_operator":
                    with self.assertRaisesRegex(opus.OpusResponseError, "invalid_concern_question"):
                        opus.review(vendor, case)
                    self.assertEqual(audit, ["invalid_concern_question"])
                else:
                    verdict = opus.review(vendor, case)
                    self.assertEqual(verdict["decision"], action)
                    self.assertEqual(verdict["concern"]["decision"],
                                     "watch" if concern == "keep_open" else "ask_operator")
                self.assertIn("at most one operator question", vendor.call_args.args[1]["system"])

    def test_outer_explanation_cannot_qualify_a_standalone_decision(self):
        case = {"concern_review": {"id": 9, "scope_tools": ["workspace_write"]}}
        # The legacy reviewer added an explanatory paragraph before its child in the trial.
        leaf = '<concern id="9" decision="close_resolved">The training note was benign.</concern>'
        for xml in ('<ruling>This case can be closed. It was a routine progress record.\n'+leaf+'\n</ruling>',
                    '<ruling>Continue monitoring this concern.\n'+leaf+'\n</ruling>',
                    '<ruling>'+leaf+'</ruling>', leaf+'\nBut only after operator approval.'):
            with self.subTest(xml=xml), self.assertRaises(opus.OpusResponseError):
                opus.review(Mock(return_value=response(xml)), case)

    def test_heading_cannot_hide_conditions_or_supply_another_instruction(self):
        verdict = '<review decision="allow">The request is in scope.</review>'
        heading = "Here is my review of the proposed workspace_write action:"
        for text in (
            "Allow only after Mike confirms.\n" + verdict,
            "Here is my review of the proposed read_heritage action:\n" + verdict,
            "Here is my review decision regarding the proposed workspace write pending confirmation:\n" + verdict,
            heading + "\n" + verdict + "\nWait for Mike.",
            heading + "\n" + verdict + '<review decision="deny">Stop.</review>',
            heading + "\n" + verdict + verdict,
            heading + "\n<search source=\"wikipedia\">ethics</search>",
            heading + '\n<response>' + verdict + '</response>',
            heading + '\n<review decision="allow">' + verdict + '</review>',
            heading + '\n<!-- pending -->' + verdict,
        ):
            with self.subTest(text=text), patch("lantern.opus.public_search") as search:
                vendor = Mock(return_value=response(text))
                with self.assertRaises(opus.OpusResponseError):
                    opus.review(vendor, {"tool": "workspace_write"})
                vendor.assert_called_once()
                search.assert_not_called()

    def test_invalid_or_partial_output_never_authorizes(self):
        valid = '<review decision="allow">Safe on this evidence.</review>'
        self.assertEqual(opus.review(Mock(return_value=response(valid)), {})["decision"], "allow")
        bad = [
            {**response(valid), "stop_reason": "max_tokens"},
            {**response(valid), "model": "another-model"},
            response('<review decision="allow">Fine</review>' * 2),
            response('<review decision="allow" once="yes">Fine</review>'),
            response('<review decision="allow"><nested>Fine</nested></review>'),
            response('<review decision="allow">   </review>'),
            response('<!DOCTYPE x [<!ENTITY x "yes">]><review decision="allow">&x;</review>'),
            response('<search source="https://some-site.example">topic</search>'),
            response('<review decision="allow">'),
        ]
        for reply in bad:
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                opus.review(Mock(return_value=reply), {})

    def test_queries_cannot_copy_long_case_phrases_or_loop_forever(self):
        call = response('<search source="wikipedia">unpublished project code name</search>')
        vendor = Mock(return_value=call)
        with patch("lantern.opus.public_search") as search, self.assertRaises(ValueError):
            opus.review(vendor, {"summary": "The unpublished project code name must stay private."})
        search.assert_not_called()
        self.assertEqual(vendor.call_count, opus.MAX_SEARCHES + 1)
        self.assertIn('"error"', vendor.call_args.args[1]["messages"][-1]["content"])

    def test_private_query_rejected_before_network(self):
        for query in ("sk-fake-private-key", "https://example.test", "person@example.test",
                      "localhost:1234567", "example.test", "a" * 161):
            with self.subTest(query=query), patch("lantern.opus._json_get") as get, self.assertRaises(ValueError):
                opus.public_search("wikipedia", query)
            get.assert_not_called()

    def test_advice_format_repair_is_bounded(self):
        malformed = response("A preamble. <advice>Ask first.</advice>")
        vendor = Mock(side_effect=[malformed, response("<advice>Ask first.</advice>")])
        self.assertEqual(opus.consult(vendor, {})["advice"], "Ask first.")
        vendor = Mock(return_value=malformed)
        with self.assertRaises(ValueError):
            opus.consult(vendor, {})
        self.assertEqual(2, vendor.call_count)

    def test_validation_belongs_to_each_individual_reply(self):
        outcomes = [[], [], []]
        vendor = Mock(side_effect=[
            opus.AuditedReply(response('private preamble <advice>Proceed</advice>'), outcomes[0].append),
            opus.AuditedReply(response('<search source="crossref">ethics</search>'), outcomes[1].append),
            opus.AuditedReply(response('<advice>Ask for consent.</advice>'), outcomes[2].append),
        ])
        with patch("lantern.opus.public_search", return_value=[]):
            opus.consult(vendor, {})
        self.assertEqual(outcomes, [["malformed_xml"], ["search"], ["advice"]])

        for reply, expected in [
            (response('private text <review decision="allow">Yes</review>'), "malformed_xml"),
            (response('<review decision="allow"><nested>Yes</nested></review>'), "nested_elements"),
            (response('<advice>Yes</advice>'), "invalid_verdict"),
            ({**response('private truncated text'), "stop_reason": "max_tokens"}, "output_limit"),
        ]:
            observed = []
            vendor = Mock(return_value=opus.AuditedReply(reply, observed.append))
            with self.subTest(expected=expected), self.assertRaises(opus.OpusResponseError) as failure:
                opus.review(vendor, {})
            self.assertEqual(failure.exception.code, expected)
            self.assertEqual(observed, [expected])
            self.assertNotIn("private", str(failure.exception))
            self.assertEqual(vendor.call_count, 1)

    def test_public_research_only_uses_fixed_hosts_and_sanitizes_snippets(self):
        with patch("lantern.opus._json_get", return_value={"query": {"search": [
            {"title": "Privacy", "snippet": 'A <span class="searchmatch">privacy</span> topic.'}
        ]}}) as get:
            results = opus.public_search("wikipedia", "Privacy law")
        host, path = get.call_args.args
        self.assertEqual(host, "en.wikipedia.org")
        self.assertIn("srsearch=Privacy+law", path)
        self.assertEqual(results, [{"title": "Privacy", "snippet": "A privacy topic.",
                                    "url": "https://en.wikipedia.org/wiki/Privacy"}])
        with patch("lantern.opus._json_get", return_value={"message": {"items": [
            {"title": ["Privacy"], "DOI": "10.example/one", "published": {"date-parts": [[2020]]}}
        ]}}) as get:
            paper, = opus.public_search("crossref", "Privacy law")
        self.assertEqual(get.call_args.args[0], "api.crossref.org")
        self.assertEqual(paper["url"], "https://doi.org/10.example/one")

    def test_research_client_refuses_redirects(self):
        connection = MagicMock()
        connection.getresponse.return_value.__enter__.return_value.status = 302
        with patch("lantern.opus.http.client.HTTPSConnection", return_value=connection) as connect:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                opus.public_search("wikipedia", "Privacy")
        connect.assert_called_once_with("en.wikipedia.org", timeout=8)
        connection.close.assert_called_once()
