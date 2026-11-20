from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock

from lantern import concerns
from lantern.chess_tool import CHESS_TOOL
from lantern.opus import OpusCaseTooLarge, OpusResponseError
from lantern.review_vendors import HTTPRejection, OpusTransportError, ReviewBudgetUnavailable
from lantern.supervision import CONSULT, ReviewUnavailable, Supervisor, review_summary, supervised_tools


def report(issue=""):
    return {"escalate": bool(issue), "summary": "One proposed action.", "evidence": issue}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)
        self.luna, self.review, self.consult = Mock(), Mock(), Mock()
        self.luna.screen.return_value = report()
        self.luna.screen.side_effect = self.screen
        self.review.return_value = {"decision": "allow", "reason": "Scoped to this test position."}
        self.review.side_effect = self.ruling
        self.consult.return_value = {"advice": "Ask the operator before sharing their notes.",
                                     "available_research": ["wikipedia", "crossref"],
                                     "research": [{"source": "crossref", "status": "ok", "hits": 2}]}
        self.execute = Mock(return_value={"move": "Qg7#"})
        self.args = {"fen": "position", "rationale": "Check this chess position for the operator."}
        self.payload = {"instructions": "host instructions", "input": [
            {"type": "reasoning", "encrypted_content": "opaque-provider-field"},
            {"role": "user", "content": [{"type": "input_text", "text": "Please check the board"}]},
            {"type": "function_call_output", "output": "An earlier tool recommended proceeding carefully."},
        ], "tools": [{"name": "chess_analyze"}]}
        self.gate = self.session()

    def screen(self, packet):
        scan = dict(self.luna.screen.return_value)
        if "concern_history" in packet:
            scan["repeat_id"] = None
        if "scope_checks" in packet:
            scan["applicable_ids"] = [row["id"] for row in packet["scope_checks"]]
        return scan

    def ruling(self, case, disposition="watch"):
        verdict = dict(self.review.return_value) if "tool" in case else {}
        if "concern_review" in case:
            verdict["concern"] = {"id": case["concern_review"]["id"], "decision": disposition,
                                  "reason": "Fixture ruling on this evidence.",
                                  "tools": [] if disposition == "retire" else ["*"]}
        return verdict

    def session(self, *, interval=12, thread="thread", batch_id=None):
        gate = Supervisor(self.state, self.luna, self.review, self.consult,
                          sample_every=interval, batch_id=batch_id)
        gate.bind(thread)
        return gate

    def flags(self):
        with closing(sqlite3.connect(self.gate.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM luna_flags ORDER BY id")]

    def call(self, call_id="call", args=None, gate=None):
        return (gate or self.gate).dispatch(call_id, "chess_analyze",
                                            self.args if args is None else args, self.execute)

    def test_selective_context_and_tool_screen_exact_action(self):
        self.gate.observe(self.payload)
        self.assertEqual(self.call(), {"move": "Qg7#"})
        self.review.assert_not_called()
        self.execute.assert_called_once_with("chess_analyze", {"fen": "position"})
        spot, action = [call.args[0] for call in self.luna.screen.call_args_list]
        self.assertEqual(spot["context"]["input"], self.payload["input"][1:])
        self.assertEqual(spot["context"]["instructions"], self.payload["instructions"])
        self.assertEqual(spot["context"]["tools"], self.payload["tools"])
        self.assertEqual(action["rationale"], self.args["rationale"])
        self.assertEqual(action["arguments"], {"fen": "position"})
        self.assertIn("Please check", action["recent_context"])
        self.assertIn("proceeding carefully", action["recent_context"])
        self.assertNotIn("opaque-provider-field", str(action))
        self.assertEqual(self.flags(), [])

    def test_flag_explanations_survive_ruling_failure_retries_and_retirement(self):
        gate = self.session(interval=1, batch_id=7)
        context_report = {"escalate": True, "evidence": "Context evidence — one.\nTwo.",
                          "summary": "Context summary."}
        tool_report = {"escalate": True, "evidence": "Action evidence: “three”.",
                       "summary": "Tool summary.\nSecond line."}
        self.luna.screen.return_value = context_report
        gate.observe(self.payload)
        self.luna.screen.return_value = tool_report
        snapshots_at_ruling = []

        def unavailable(case):
            snapshots_at_ruling.append(self.flags())
            raise TimeoutError()

        self.review.side_effect = unavailable
        held = self.call(gate=gate)
        self.assertIn("held", held["error"])
        self.assertEqual(self.call(gate=gate), held)
        self.review.side_effect = lambda case: self.ruling(case, "retire")
        self.luna.screen.return_value = report()
        resumed = self.session(interval=1)
        resumed.observe(self.payload)
        self.assertIn("move", self.call("later", gate=resumed))
        resumed.observe(self.payload)
        with closing(sqlite3.connect(self.gate.path)) as db:
            self.assertEqual(concerns.list_open(db, "thread"), [])
            receipt, = concerns.history(db, "thread")
            self.assertEqual(receipt["status"], "retired")
        rows = self.flags()
        self.assertEqual(snapshots_at_ruling, [rows])
        self.assertEqual([(r["thread"], r["batch_id"], r["sequence"], r["kind"], r["tool"], r["call_id"],
                           r["evidence"], r["summary"]) for r in rows], [
            ("thread", 7, 1, "context", None, None, context_report["evidence"], context_report["summary"]),
            ("thread", 7, 1, "tool", "chess_analyze", "call", tool_report["evidence"], tool_report["summary"]),
        ])
        for row in rows:
            self.assertEqual(datetime.fromisoformat(row["at"]).utcoffset(), timedelta(0))
        self.assertNotIn("opaque-provider-field", json.dumps(rows))
        self.assertNotIn(self.args["rationale"], json.dumps(rows))

    def test_reviewed_history_covers_allowed_and_denied_calls_in_one_thread(self):
        self.gate.observe(self.payload)
        self.assertIn("move", self.call("old-allow"))
        self.luna.screen.return_value = report("Fixture concern")
        self.review.return_value = {"decision": "deny", "reason": "Fixture denial"}
        self.assertIn("error", self.call("old-deny"))
        self.gate.unregistered("never_registered", "{}")
        self.assertEqual(self.gate.reviewed_calls(), {("chess_analyze", "old-allow"), ("chess_analyze", "old-deny")})
        self.assertEqual(self.session(thread="other").reviewed_calls(), set())

    def test_first_audit_on_a_preexisting_operator_database(self):
        legacy = self.state / "legacy"
        legacy.mkdir()
        with closing(sqlite3.connect(legacy / "supervision.sqlite")) as db, db:
            db.execute("CREATE TABLE events(at TEXT DEFAULT CURRENT_TIMESTAMP, thread TEXT NOT NULL,"
                       "event TEXT NOT NULL, tool TEXT, call_id TEXT)")
            db.execute("INSERT INTO events(thread,event,tool,call_id) VALUES('old','tool_flagged','chess_analyze','x')")
        self.state = legacy
        self.gate = self.session()
        self.assertEqual(review_summary(self.state), {"tool_flagged": 1})
        self.assertEqual(self.flags(), [])
        self.gate.observe(self.payload)
        self.luna.screen.return_value = report("Luna requests clarification before this tool.")
        self.assertIn("rationale", self.call(args={})["error"])
        flag, = self.flags()
        self.assertEqual(flag["evidence"], self.luna.screen.return_value["evidence"])
        self.assertIsNone(flag["batch_id"])
        self.review.assert_not_called()

    def test_flagged_action_passes_evidence_and_opus_denial_to_model(self):
        self.gate.observe(self.payload)
        self.luna.screen.return_value = report("Concern about authorization.")
        self.review.return_value = {"decision": "deny", "reason": "Only share records with the owner's consent."}
        result = self.call()
        case, = self.review.call_args.args
        self.assertEqual(case["arguments"], {"fen": "position"})
        self.assertEqual(case["evidence"], "Concern about authorization.")
        self.assertEqual(case["rationale"], self.args["rationale"])
        self.assertIn("error", result)
        self.assertEqual(result["judgment"], self.review.return_value["reason"])
        self.execute.assert_not_called()
        self.review.return_value = {"decision": "allow", "reason": "Accept the revised proposal."}
        self.assertEqual(self.call("new", {"fen": "different", "rationale": "Operator requested it."}), {"move": "Qg7#"})
        self.execute.assert_called_once_with("chess_analyze", {"fen": "different"})
        counts = review_summary(self.state)
        self.assertEqual((counts["opus_allowed"], counts["opus_denied"]), (1, 1))

    def test_only_an_explicit_opus_disposition_retires_a_concern(self):
        gate = self.session(interval=1)
        self.luna.screen.return_value = report("Unresolved context concern.")
        gate.observe(self.payload)
        initial = self.review.call_args.args[0]
        self.assertNotIn("tool", initial)
        self.assertEqual(initial["concern_review"]["evidence"], "Unresolved context concern.")
        self.luna.screen.return_value = report()
        gate = self.session(interval=1)
        gate.observe(self.payload)
        self.assertEqual(self.call(gate=gate), {"move": "Qg7#"})
        self.assertEqual(self.review.call_args.args[0]["concern_review"]["id"], initial["concern_review"]["id"])
        self.assertEqual(self.review.call_count, 2)
        self.review.side_effect = lambda case: self.ruling(case, "retire")
        self.assertIn("move", self.call("again", gate=gate))
        self.assertEqual(self.review.call_count, 3)
        gate.observe(self.payload)
        self.assertIn("move", self.call("after-retirement", gate=gate))
        self.assertEqual(self.review.call_count, 3)

    def test_cadence_survives_restart_and_ctx_outage_does_not_skip_required_scan(self):
        for _ in range(3):
            self.session(interval=3).observe(self.payload)
        self.assertEqual(self.luna.screen.call_count, 1)
        gate = self.session(interval=3)
        self.luna.screen.side_effect = TimeoutError()
        with self.assertRaises(ReviewUnavailable):
            gate.observe(self.payload)
        self.assertIn("error", self.call(gate=gate))
        self.luna.screen.side_effect = None
        gate = self.session(interval=3)
        gate.observe(self.payload)
        self.assertEqual(self.luna.screen.call_count, 3)
        self.session(thread="second").observe(self.payload)
        self.assertEqual(self.luna.screen.call_count, 4)

    def test_reviewer_unavailable_and_malformed_rationale_do_not_execute(self):
        self.gate.observe(self.payload)
        self.assertIn("rationale", self.call(args={"fen": "position"})["error"])
        self.assertIsNone(self.luna.screen.call_args.args[0]["rationale"])
        self.luna.screen.side_effect = RuntimeError("private vendor failure text")
        self.assertNotIn("private", self.call("unavailable")["error"])
        self.luna.screen.side_effect = None
        self.luna.screen.return_value = report("Needs review.")
        self.review.return_value = {"decision": "approved-ish", "reason": "maybe"}
        message = self.call("bad-verdict")["error"]
        self.assertIn("unreadable (invalid_verdict)", message)
        self.assertIn("inspect the archived reply.", message)
        self.execute.assert_not_called()

    def test_unreadable_review_reports_original_turn_on_replay(self):
        gate = self.session(batch_id=61)
        gate.observe(self.payload)
        self.luna.screen.return_value = report("Needs review.")
        self.review.side_effect = OpusResponseError("malformed_xml")
        result = self.call(gate=gate)
        self.assertIn("did not run and remains held", result["error"])
        self.assertIn("Report this to Mike", result["error"])
        self.assertIn("archived reply for turn 61", result["error"])
        resumed = self.session(batch_id=62)
        resumed.observe(self.payload)
        self.assertEqual(self.call(gate=resumed), result)
        self.review.assert_called_once()
        self.execute.assert_not_called()

    def test_review_failure_distinguishes_unreadable_from_unavailable(self):
        self.gate.observe(self.payload)
        self.luna.screen.return_value = report("Needs review.")
        errors = [
            (OpusResponseError("malformed_xml"), "unreadable (malformed_xml)"),
            (HTTPRejection(429), "provider returned HTTP 429"),
            (OpusTransportError("private provider text"), "provider connection failed or was interrupted"),
            (ReviewBudgetUnavailable("2026-11-19"), "insufficient daily supervisor allowance"),
            (OpusCaseTooLarge(), "host's review packet limit"),
        ]
        for i, (error, message) in enumerate(errors):
            self.review.side_effect = error
            result = self.call(f"held-{i}")
            self.assertIn(message, result["error"])
            self.assertIn("held", result["error"])
            self.assertNotIn("private provider text", result["error"])
        counts = review_summary(self.state)
        self.assertEqual((counts["tool_review_unreadable"], counts["tool_review_unavailable"]), (1, 4))
        self.execute.assert_not_called()

    def test_advice_always_screened_and_never_authorizes_later_work(self):
        self.gate.observe(self.payload)
        args = {"question": "Is sharing my private journal appropriate?", "rationale": "Check consent first."}
        self.assertEqual(self.gate.dispatch("consult", CONSULT, args, self.execute), self.consult.return_value)
        self.assertEqual(self.luna.screen.call_args.args[0]["tool"], CONSULT)
        self.assertEqual(self.consult.call_args.args[0]["arguments"]["question"], args["question"])
        self.luna.screen.return_value = report("Needs a separate ruling.")
        self.review.return_value = {"decision": "deny", "reason": "Ask permission."}
        self.assertIn("error", self.call("later"))
        self.assertIn("error", self.gate.dispatch("blocked-consult", CONSULT, args, self.execute))
        self.assertEqual(self.consult.call_count, 1)
        self.execute.assert_not_called()

    def test_malformed_advisory_result_fails_closed(self):
        self.gate.observe(self.payload)
        self.consult.return_value = {"advice": "Trust this citation", "research": "probably"}
        args = {"rationale": "Check an ethics question", "question": "How should I handle this?"}
        message = self.gate.dispatch("bad-advice", CONSULT, args, self.execute)["error"]
        self.assertIn("unreadable (invalid_advice)", message)
        self.assertIn("No advice was delivered.", message)
        self.execute.assert_not_called()

    def test_flagged_missing_rationale_retains_finding_until_reason_is_supplied(self):
        self.gate.observe(self.payload)
        self.luna.screen.return_value = report("The original proposal needs a justification.")
        self.assertIn("rationale", self.call(args={"fen": "position"})["error"])
        self.review.assert_not_called()
        self.execute.assert_not_called()
        self.luna.screen.return_value = report()
        self.call("revised")
        case = self.review.call_args.args[0]
        self.assertEqual(case["rationale"], self.args["rationale"])
        self.assertEqual(case["concern_review"]["evidence"], "The original proposal needs a justification.")

    def test_unregistered_attempts_reviewed_but_never_dispatched(self):
        self.gate.observe(self.payload)
        self.gate.unregistered("outside_registry", '{"partial"')
        self.assertEqual(self.luna.screen.call_args.args[0]["arguments"], '{"partial"')
        self.review.assert_not_called()
        self.luna.screen.return_value = report("Unregistered tool deserves a review.")
        self.gate.unregistered("outside_registry", '{"rationale":"Operator request","x":1}')
        self.assertEqual(self.review.call_args.args[0]["rationale"], "Operator request")
        self.assertEqual(self.review.call_args.args[0]["arguments"]["x"], 1)
        self.assertEqual(review_summary(self.state)["unregistered_blocked"], 2)
        self.assertEqual(review_summary(self.state)["unregistered_opus_allow"], 1)
        self.execute.assert_not_called()

    def test_unavailable_unregistered_review_survives_next_clean_spot_check(self):
        gate = self.session(interval=1)
        self.luna.screen.return_value = report("Original context concern.")
        gate.observe(self.payload)
        self.luna.screen.return_value = report("Unregistered tool concern.")
        self.review.side_effect = TimeoutError()
        with self.assertRaises(ReviewUnavailable):
            gate.unregistered("outside_registry", None)
        self.review.side_effect = self.ruling
        self.luna.screen.return_value = report()
        gate = self.session(interval=1)
        gate.observe(self.payload)
        self.assertIn("move", self.call("registered", gate=gate))
        cases = [call.args[0]["concern_review"] for call in self.review.call_args_list
                 if call.args[0].get("tool") == "chess_analyze"]
        self.assertEqual([case["evidence"] for case in cases], ["Original context concern.", "Unregistered tool concern."])
        self.assertNotEqual(cases[0]["id"], cases[1]["id"])

    def test_overlapping_observations_and_a_late_unregistered_proposal(self):
        gate = self.session(interval=1)
        old_scanning, new_started, new_scanning = Event(), Event(), Event()
        release_old, release_new = Event(), Event()
        review_packets = []

        def screen(packet):
            if packet["kind"] == "context":
                if packet["context"]["input"] == "older":
                    old_scanning.set()
                    self.assertTrue(release_old.wait(5))
                else:
                    new_scanning.set()
                    self.assertTrue(release_new.wait(5))
            else:
                review_packets.append((packet["tool"], packet["recent_context"]))
                if packet["tool"] == "unavailable":
                    return report("The unregistered proposal followed the older input.")
            return report()

        def old_request():
            gate.observe({"input": "older"})
            self.assertTrue(new_scanning.wait(5))
            gate.unregistered("unavailable", "{}")

        def new_request():
            new_started.set()
            gate.observe({"input": "new steering"})

        self.luna.screen.side_effect = screen
        with ThreadPoolExecutor(max_workers=2) as workers:
            old = workers.submit(old_request)
            try:
                self.assertTrue(old_scanning.wait(5))
                new = workers.submit(new_request)
                self.assertTrue(new_started.wait(5))
                self.assertFalse(new_scanning.wait(.1), "Later observation overtook a pending scan")
                release_old.set()
                old.result(timeout=5)
                self.assertIn("held", self.call("pending", gate=gate)["error"])
            finally:
                release_old.set()
                release_new.set()
            new.result(timeout=5)
        self.assertEqual({"move": "Qg7#"}, self.call("latest", gate=gate))
        self.assertEqual([("unavailable", "older"), ("chess_analyze", "new steering")], review_packets)
        self.assertEqual(1, review_summary(self.state)["unregistered_blocked"])
        flag, = self.flags()
        self.assertEqual((flag["sequence"], flag["kind"], flag["tool"], flag["call_id"]),
                         (1, "unregistered_tool", "unregistered", None))
        self.assertEqual(flag["evidence"], "The unregistered proposal followed the older input.")
        self.execute.assert_called_once()

    def test_late_finding_is_reviewed_even_if_it_arrives_during_an_opus_ruling(self):
        gate = self.session(interval=1)
        self.luna.screen.return_value = report("Context concern")
        gate.observe(self.payload)
        self.luna.screen.return_value = report()
        ruling, release = Event(), Event()

        def review(case):
            if case["tool"] == "outside":
                raise TimeoutError()
            ruling.set()
            self.assertTrue(release.wait(5))
            return self.ruling(case, "retire")

        self.review.side_effect = review
        with ThreadPoolExecutor(max_workers=1) as workers:
            action = workers.submit(self.call, "earlier", None, gate)
            try:
                self.assertTrue(ruling.wait(5))
                self.luna.screen.return_value = report("Late evidence")
                with self.assertRaises(ReviewUnavailable):
                    gate.unregistered("outside", "{}")
            finally:
                release.set()
            action.result(timeout=5)
        self.luna.screen.return_value = report()
        gate.observe({"input": "Clean later context"})
        self.call("later", gate=gate)
        case = self.review.call_args.args[0]
        self.assertEqual("chess_analyze", case["tool"])
        self.assertIn("Late evidence", case["concern_review"]["evidence"])

    def test_retry_and_conflicting_call_ids(self):
        self.gate.observe(self.payload)
        entered, proceed = Event(), Event()

        def execute_once(*_):
            entered.set()
            self.assertTrue(proceed.wait(timeout=3))
            return {"ack": True}

        with ThreadPoolExecutor(max_workers=1) as workers:
            first = workers.submit(self.gate.dispatch, "id", "chess_analyze", self.args, execute_once)
            try:
                self.assertTrue(entered.wait(timeout=3))
                self.assertIn("in progress", self.call("id")["error"])
                self.assertIn("different", self.call("id", {**self.args, "rationale": "changed"})["error"])
            finally:
                proceed.set()
            self.assertEqual(first.result(), {"ack": True})
        restarted = self.session()
        restarted.observe(self.payload)
        self.assertEqual(self.call("id", gate=restarted), {"ack": True})
        self.execute.assert_not_called()
        self.assertEqual(review_summary(self.state)["tool_clear"], 1)

    def test_supervised_tool_schemas(self):
        schemas = supervised_tools([CHESS_TOOL])
        self.assertEqual([tool["name"] for tool in schemas], ["chess_analyze", "consult_opus"])
        self.assertTrue(all("rationale" in tool["inputSchema"]["required"] for tool in schemas))
        self.assertNotIn("rationale", CHESS_TOOL["inputSchema"]["properties"])

    def test_large_backlog_does_not_silently_truncate_unreviewed_findings(self):
        self.gate.observe(self.payload)
        for i in range(40):
            self.luna.screen.return_value = report(f"Independent concern {i}.")
            self.assertIn("rationale", self.call(f"missing-{i}", args={"fen": "position"})["error"])
        self.execute.assert_not_called()
        self.luna.screen.return_value = report()
        reviewed = []

        def judge(case):
            self.execute.assert_not_called()
            self.assertLess(len(json.dumps(case, ensure_ascii=False).encode()), 64 * 1024)
            reviewed.append(case["concern_review"])
            return self.ruling(case, "retire")

        self.review.side_effect = judge
        self.assertIn("move", self.call("complete"))
        self.assertEqual(len({finding["id"] for finding in reviewed}), 40)
        self.assertEqual([finding["evidence"] for finding in reviewed], [f"Independent concern {i}." for i in range(40)])
        self.execute.assert_called_once()
