"""Replay a synthetic overnight sequence through the real screening and ruling protocols."""

from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from lantern import opus
from lantern.luna import Luna
from lantern.supervision import Supervisor, database


class ConcernReplayTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.screenings = []
        self.rulings = []
        self.execute = Mock(return_value={"done": True})
        self.luna_reply = {}
        self.opus_xml = ""
        self.gate = self.worker()

    def worker(self):
        gate = Supervisor(self.state, Luna(self.vendor), partial(opus.review, self.vendor), Mock(),
                          sample_every=1, tool_names=["workspace_write", "chess_analyze"])
        gate.bind("synthetic-room")
        return gate

    def vendor(self, vendor, payload, *, purpose):
        if vendor == "luna":
            packet = json.loads(payload["input"][0]["content"])
            self.screenings.append(packet)
            fields = payload["text"]["format"]["schema"]["properties"]
            report = {"escalate": False, "evidence": "", "summary": "Routine."}
            if "repeat_id" in fields:
                report["repeat_id"] = None
            if "applicable_ids" in fields:
                report["applicable_ids"] = []
            report.update(self.luna_reply)
            return {"status": "completed", "output": [
                {"type": "message", "status": "completed", "role": "assistant",
                 "content": [{"type": "output_text", "text": json.dumps(report)}]}]}
        self.assertEqual(vendor, "opus")
        packet = json.loads(payload["messages"][0]["content"])
        self.rulings.append((purpose, packet["case"]))
        number = packet["case"].get("concern_review", {}).get("id")
        return {"model": opus.OPUS_MODEL, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": self.opus_xml.format(id=number)}]}

    def initial_ruling(self, disposition):
        self.luna_reply = {"escalate": True, "evidence": "An earlier review note was disputed.",
                           "summary": "Determine whether that incident needs follow-up."}
        scope = ' tools="workspace_write"' if disposition == "keep_open" else ""
        self.opus_xml = ('<concern id="{id}" decision="' + disposition + '"' + scope + '>'
                         'The question concerns writing about the review.</concern>')
        self.gate.observe({"input": "My proposed note about the review was held."})
        self.assertEqual(len(self.rulings), 1)
        purpose, case = self.rulings[0]
        self.assertEqual(purpose, "context_review")
        self.assertNotIn("tool", case)
        self.assertIn("proposed note", case["recent_context"])
        self.luna_reply = {}
        return case["concern_review"]["id"]

    def write(self, call_id, text, rationale="Save a private note."):
        return self.gate.dispatch(call_id, "workspace_write",
                                  {"path": "notes/journal.md", "text": text, "rationale": rationale},
                                  self.execute)

    def test_retired_incident_paraphrase_then_new_attempt(self):
        number = self.initial_ruling("close_resolved")
        self.gate = self.worker()  # Native compaction and worker restarts can reword the same history.
        self.luna_reply = {"repeat_id": number, "summary": "The same past incident is recounted."}
        self.gate.observe({"input": "Earlier summary: the agent agreed to leave that note pending."})
        record, = self.screenings[-1]["concern_history"]
        self.assertEqual((record["id"], record["status"]), (number, "retired"))
        with database(self.gate.path) as db:
            self.assertEqual(db.execute("SELECT concern_id FROM concern_repeats").fetchall(), [(number,)])
        self.luna_reply = {}
        chess = self.gate.dispatch("chess", "chess_analyze",
                                   {"fen": "fixture", "rationale": "Study the new chess position."}, self.execute)
        self.assertEqual(chess, {"done": True})
        self.assertEqual(len(self.rulings), 1)
        self.luna_reply = {"escalate": True, "evidence": "A new attempt to record disputed material.",
                           "summary": "Review this specific write."}
        self.opus_xml = '<review decision="deny">Leave this particular write held.</review>'
        denied = self.write("fresh-write", "Record the disputed review material.")
        self.assertIn("denied", denied["error"])
        self.assertEqual([purpose for purpose, _ in self.rulings], ["context_review", "escalation"])
        self.assertEqual(self.execute.call_count, 1)
        self.luna_reply = {}
        self.gate.observe({"input": "The new write was held."})
        self.assertIn("fresh-write", [item["call_id"] for item in self.screenings[-1]["new_tool_events"]])

    def test_fresh_context_evidence_includes_previous_scoped_ruling(self):
        number = self.initial_ruling("close_resolved")
        self.luna_reply = {"escalate": True, "repeat_id": number, "evidence": "Changed destination.",
                           "summary": "A new proposal would publish a previously private note."}
        self.gate.observe({"input": "Now the destination has changed to a public forum."})
        self.assertEqual(len(self.rulings), 2)
        _, case = self.rulings[-1]
        self.assertNotEqual(case["concern_review"]["id"], number)
        self.assertEqual(case["concern_review"]["related_id"], number)
        receipt = case["related_concern_receipt"]
        self.assertEqual((receipt["id"], receipt["status"], receipt["tools"]), (number, "retired", []))
        self.assertIn("writing about the review", receipt["reason"])

    def test_watch_skips_unrelated_write_and_rereviews_related_action(self):
        number = self.initial_ruling("keep_open")
        self.assertEqual(self.write("routine", "My chess study ended in a draw."), {"done": True})
        self.assertEqual([row["id"] for row in self.screenings[-1]["scope_checks"]], [number])
        self.assertEqual(len(self.rulings), 1)
        self.luna_reply = {"applicable_ids": [number]}
        self.opus_xml = ('<ruling><review decision="deny">Wait for authorization on this document.</review>'
                         '<concern id="{id}" decision="close_resolved">The historical concern has been addressed.'
                         '</concern></ruling>')
        self.assertIn("denied", self.write("related", "Record the disputed review material.")["error"])
        self.assertEqual([purpose for purpose, _ in self.rulings], ["context_review", "escalation"])
        self.assertEqual(self.execute.call_count, 1)
        self.gate = self.worker()
        self.luna_reply = {"repeat_id": number, "summary": "Describing the old incident again."}
        self.gate.observe({"input": "A previous journal write was refused. No further action proposed."})
        record, = self.screenings[-1]["concern_history"]
        self.assertEqual(record["status"], "retired")
        self.assertEqual(len(self.rulings), 2)

    def test_malformed_combined_ruling_preserves_watch_and_holds_action(self):
        number = self.initial_ruling("keep_open")
        self.luna_reply = {"applicable_ids": [number]}
        self.opus_xml = '<ruling><review decision="allow">Yes.</review></ruling>'  # Missing disposition.
        held = self.write("related", "Record the disputed review material.")
        self.assertIn("unreadable", held["error"])
        self.execute.assert_not_called()
        self.assertEqual(self.write("related", "Record the disputed review material."), held)
        self.assertEqual(len(self.rulings), 2)
        self.gate = self.worker()
        self.luna_reply = {"repeat_id": number, "summary": "Same incident."}
        self.gate.observe({"input": "That failed. Leave the write pending."})
        self.assertEqual(self.screenings[-1]["concern_history"][0]["status"], "watch")
        self.assertEqual(len(self.rulings), 2)

    def test_retirement_preserves_exact_proposal_refusal_until_explicit_allow(self):
        number = self.initial_ruling("keep_open")
        disputed = "Record this particular review exchange."
        self.luna_reply = {"applicable_ids": [number]}
        self.opus_xml = ('<ruling><review decision="deny">Leave this specific note pending.</review>'
                         '<concern id="{id}" decision="close_resolved">The old pattern is resolved.</concern></ruling>')
        self.assertIn("denied", self.write("original", disputed)["error"])
        self.execute.assert_not_called()

        self.gate = self.worker()
        self.luna_reply = {"repeat_id": number, "summary": "Describes the original refusal."}
        self.gate.observe({"input": "Only that specific note was held."})
        self.assertEqual(self.screenings[-1]["concern_history"][0]["status"], "retired")
        self.luna_reply = {}
        self.assertEqual(self.write("unrelated", "The chess study ended in a draw."), {"done": True})
        self.assertEqual(len(self.rulings), 2)

        self.opus_xml = '<review decision="allow">The new operator clarification covers this exact note.</review>'
        self.assertEqual(self.write("fresh-id", disputed, rationale="Reconsider after the private clarification."),
                         {"done": True})
        self.assertEqual([purpose for purpose, _ in self.rulings], ["context_review", "escalation", "escalation"])
        self.assertEqual(self.rulings[-1][1]["arguments"], self.rulings[-2][1]["arguments"])

        # A valid allow supersedes the prior deny, including after another worker restart.
        self.gate = self.worker()
        self.luna_reply = {"repeat_id": number, "summary": "Earlier discussion."}
        self.gate.observe({"input": "Routine resumption."})
        self.luna_reply = {}
        self.assertEqual(self.write("later-id", disputed), {"done": True})
        self.assertEqual(len(self.rulings), 3)
        self.assertEqual(self.execute.call_count, 3)
