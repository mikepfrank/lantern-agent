from datetime import datetime, timezone
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from lantern import opus


def response(text):
    return {"model": opus.OPUS_MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}]}


class OpusTests(unittest.TestCase):
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
