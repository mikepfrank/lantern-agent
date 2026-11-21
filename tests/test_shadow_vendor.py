from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern import shadow_vendor as vendor
from lantern.budget import units
from lantern.review_vendors import HTTPRejection, ReviewerVendors
from lantern.review_policy import ACTION_FACTS_POLICY, OPERATOR_QUESTION_POLICY, STANDING_POLICY


class ShadowVendorTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", ANTHROPIC_API_KEY="test"))
        self.limit = Decimal(5)
        self.now = datetime.now(timezone.utc)
        self.deadline = self.now + timedelta(days=7)
        self.ledger = ReviewerVendors(self.state, self.limit)
        self.post = self.enterContext(patch("lantern.shadow_vendor.request_json"))
        self.action = {"decision": "allow", "reason": "Within Mike's authorization."}
        self.input = {"case": {"tool": "read_heritage"}, "host_facts": {"operator_statements": []}}
        self.sample = {"case_text": json.dumps(self.input), "can_ask": True, "can_ask_concern": True,
                       "operator_context": "HOST-CONTEXT", "utc_date": "2026-11-21"}
        self.reply(self.action)

    def reply(self, value):
        self.post.return_value = {"content": [{"type": "text", "text": json.dumps(value)}],
                                  "model": vendor.MODEL, "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 300, "output_tokens": 30}}

    def call(self, n=1, **updates):
        opts = {"request_id": f"{n:032x}", "sample": self.sample, "daily_limit": self.limit,
                "deadline": self.deadline, "thread_id": "room", "batch_id": 10, "tool": "read_heritage"}
        return vendor.call_sonnet(self.state, **{**opts, **updates})

    def test_exact_evidence_policies_structured_output_and_real_ledger(self):
        result = self.call()
        self.assertEqual(result["verdict"], self.action)
        self.assertEqual((result["status"], result["ledger_status"], Decimal(result["cost_usd"])),
                         ("ok", "metered", Decimal(".00135")))
        (name, key, body), kwargs = self.post.call_args
        self.assertEqual((name, key), ("sonnet", "test"))
        payload = json.loads(body)
        self.assertEqual(payload["model"], vendor.MODEL)
        self.assertEqual(payload["messages"], [{"role": "user", "content":self.sample["case_text"]}])
        for instruction in (ACTION_FACTS_POLICY, STANDING_POLICY, OPERATOR_QUESTION_POLICY, "HOST-CONTEXT"):
            self.assertIn(instruction, payload["system"])
        schema = payload["output_config"]["format"]["schema"]
        self.assertEqual(schema["properties"]["decision"]["enum"], ["allow", "deny","ask_operator"])
        self.assertEqual(set(schema["required"]), {"decision", "reason"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(self.call()["status"], "duplicate")
        self.post.assert_called_once()
        usage = self.ledger.today()
        self.assertEqual(usage["reserved_usd"], "0")
        self.assertEqual(usage["by_purpose"][0]["purpose"], vendor.PURPOSE)

    def test_open_and_retired_scope_in_batched_action_and_bad_ids(self):
        concerns = [{"id": 2, "scope_tools":["workspace_write"]},{"id":3,"scope_tools":["chess_analyze"]}]
        self.input["case"]["concern_reviews"] = concerns
        self.sample["case_text"] = json.dumps(self.input)
        first = {"id": 2, "decision":"keep_open","reason":"A limited write is under review.","tools":["workspace_write"]}
        other = {"id": 3, "decision":"close_resolved","reason":"The permission is clear.","tools": []}
        self.reply({**self.action, "concerns": [other, first]})
        result = self.call()["verdict"]
        self.assertEqual([c["decision"] for c in result["concerns"]], ["watch", "retire"])
        for i, (a, b) in enumerate(((first, first), ({**first, "tools":["other"]}, other),
                                  (first, {**other, "tools":["chess_analyze"]}),
                                  ({**first, "decision":"ask_operator"}, other),
                                  ({**first, "id": True}, other)), 2):
            self.reply({**self.action, "concerns": [a, b]})
            self.assertEqual(self.call(i)["status"], "invalid_output")

    def test_standalone_concern_and_final_question_eligibility(self):
        concern = {"id": 1, "scope_tools":["workspace_write"]}
        ruling = {"id": 1, "decision":"ask_operator","reason":"Should this new disclosure proceed?",
                  "tools": ["workspace_write"]}
        self.input["case"] = {"concern_review": concern}
        self.sample["case_text"] = json.dumps(self.input)
        self.reply({"concern": ruling})
        self.assertEqual(self.call()["verdict"], {"concern": ruling})
        self.input["case"]["tool"] = "workspace_write"
        self.sample["case_text"] = json.dumps(self.input)
        self.reply({"concern": ruling, "decision": "ask_operator","reason":"May this proceed?"})
        self.assertEqual(self.call(2)["status"], "invalid_output")
        self.input["concern_attestation"] = {"answer": "Allow private notes."}
        self.sample["case_text"] = json.dumps(self.input)
        self.reply({"concern": ruling, **self.action})
        self.assertEqual(self.call(3)["status"], "invalid_output")
        self.reply({"concern": {**ruling, "decision":"close_resolved","tools": []}, **self.action})
        self.assertEqual(self.call(4)["status"], "ok")

    def test_consultation_admission_has_the_same_direct_only_question_channel(self):
        self.input["case"]["tool"] = "consult_opus"
        self.sample["case_text"] = json.dumps(self.input)
        self.reply({"decision": "ask_operator", "reason": "Who is the recipient of this consultation?"})
        self.assertEqual(self.call()["verdict"]["decision"], "ask_operator")
        self.reply({"decision": "deny", "reason": "Have Mike confirm who may see these notes."})
        self.assertEqual(self.call(2)["status"], "invalid_output")

    def test_concurrent_reservations_and_deadline(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda unused: self.call(), range(3)))
        self.assertEqual(sorted(r["status"] for r in results), ["duplicate", "duplicate","ok"])
        self.assertEqual(self.call(9, deadline=self.now)["status"], "expired")
        self.post.assert_called_once()

    def test_existing_supervisor_liabilities_headroom_and_dated_grant(self):
        day = self.now.date().isoformat()
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("""INSERT INTO calls(id, at, day, vendor, purpose, reserved, cost, status)
                        VALUES('old', ?, ?, 'opus','escalation',?,NULL,'ambiguous')""",
                       (self.now.isoformat(), day, units(Decimal("4.50"))))
        self.assertEqual(self.call()["status"], "daily_headroom")
        self.post.assert_not_called()
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("INSERT INTO review_daily_grants VALUES(?,?)", (day, units(Decimal("0.50"))))
        self.assertEqual(self.call()["status"], "ok")

    def test_pilot_counts_outstanding_holds_and_charges_across_utc_days(self):
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("""INSERT INTO calls(id, at, day, vendor, purpose, reserved, status)
                          VALUES('old', 'yesterday','2020-01-01',?,?,?,'ambiguous')""",
                       (vendor.MODEL, vendor.PURPOSE, units(Decimal(".49"))))
        self.assertEqual(self.call()["status"], "pilot_cap")
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("UPDATE calls SET cost=0 WHERE id='old'")
            db.executemany("""INSERT INTO calls(id, at, day, vendor, purpose, reserved, cost, status)
                        VALUES(?, 'previous', '2020-01-01',?,?,0,0,'http_400')""",
                        [(f"old-{n}", vendor.MODEL, vendor.PURPOSE) for n in range(19)])
        self.assertEqual(self.call()["status"], "sample_limit")
        self.post.assert_not_called()

    def test_continuation_uses_total_ledger_and_preserves_live_headroom(self):
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.executemany("""INSERT INTO calls(id,at,day,vendor,purpose,reserved,cost,status)
                          VALUES(?,'prior','2026-01-01',?,?,0,?,'metered')""", [
                          (f"prior-{n}", vendor.MODEL, vendor.PURPOSE, units(Decimal(".02")))
                          for n in range(vendor.MAX_CALLS)])
        limits = {"case_limit": 30, "pilot_limit": Decimal(".90")}
        self.assertEqual(self.call()["status"], "sample_limit")
        self.assertEqual(self.call(**{**limits, "pilot_limit": Decimal(".41")})["status"], "pilot_cap")
        self.assertEqual(self.call(**{**limits, "daily_limit": Decimal(".50")})["status"], "daily_headroom")
        self.post.assert_not_called()
        self.assertEqual(self.call(**limits)["status"], "ok")
        self.assertEqual(self.call(**limits)["status"], "duplicate")
        self.post.assert_called_once()

    def test_malformed_verdict_billed_transport_ambiguity_retained_rejections_released(self):
        self.reply({"reason": "No decision."})
        errored = self.call()
        self.assertEqual((errored["status"], errored["held_usd"]), ("invalid_output", "0"))
        self.assertGreater(Decimal(errored["cost_usd"]), 0)
        self.post.return_value["content"][0]["text"] = '{"decision":"deny","decision":"allow","reason":"both"}'
        self.assertEqual(self.call(4)["status"], "invalid_output")
        self.post.side_effect = OSError("Unknown outcome")
        ambiguous = self.call(2)
        self.assertEqual(ambiguous["status"], "transport_error")
        self.assertGreater(Decimal(ambiguous["held_usd"]), 0)
        self.assertIsNone(ambiguous["cost_usd"])
        self.post.side_effect = HTTPRejection(429)
        rejected = self.call(3)
        self.assertEqual((rejected["status"], rejected["cost_usd"]), ("http_429", "0"))

    def test_standard_pricing_with_cache_creation_categories(self):
        counts, cost = vendor._charge({"input_tokens": 1000, "output_tokens": 100,
                                    "cache_creation_input_tokens": 20, "cache_read_input_tokens": 100,
                                    "cache_creation": {"ephemeral_1h_input_tokens": 10,
                                                      "ephemeral_5m_input_tokens": 10}})
        self.assertEqual(counts, (1120, 100, 100, 20))
        self.assertEqual(cost, units(Decimal(".0046275")))
