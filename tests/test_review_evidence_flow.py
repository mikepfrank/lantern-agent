"""Replay note reviews through the real screening and adjudication wire formats, offline."""

import json
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from lantern import concerns, opus
from lantern.chat_store import ChatStore
from lantern.luna import Luna
from lantern.operator_review import ReviewRoom
from lantern.review_policy import CONCERN_SOURCE_POLICY
from lantern.supervision import Supervisor, database
from lantern.workspace import WORKSPACE_EDITS, workspace_tool


class ReviewEvidenceFlow(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory())).resolve(strict=True)
        self.files = self.state / "files"
        self.files.mkdir()
        self.thread = "fixture-reading-room"
        self.store = ChatStore(self.state)
        self.room = ReviewRoom(self.store)
        self.store.submit("Please update the private test log.")
        self.batch = self.store.claim()
        self.store.bind_thread(self.batch.id, self.thread)
        self.luna_packets = []
        self.luna_instructions = []
        self.opus_packets = []
        self.observations = []
        self.flag = "Check the proposed edit against the private note policy."
        self.context_flag = ""
        self.xml = '<review decision="allow">The edit is within the recorded scope.</review>'
        self.gate = Supervisor(self.state, Luna(self.screen), self.review, Mock(), operator_room=self.room,
                               batch_id=self.batch.id, workspace_root=self.files, tool_names=WORKSPACE_EDITS)
        self.gate.bind(self.thread)
        self.execute = partial(workspace_tool, self.files)
        self.path = "notes/reading-log.md"
        self.original = "Page 1: earlier entry.\nPage 2: another saved label.\n"
        saved = self.execute("workspace_write", {"path": self.path, "text": self.original})
        self.sha = saved["sha256"]

    def screen(self, name, payload, *, purpose):
        self.assertEqual(name, "luna")
        packet = json.loads(payload["input"][0]["content"])
        self.luna_packets.append(packet)
        self.luna_instructions.append(payload["instructions"])
        evidence = self.flag if packet["kind"] == "tool" else self.context_flag
        report = {"escalate": bool(evidence), "evidence": evidence, "summary": "Synthetic review."}
        if "concern_history" in packet:
            report["repeat_id"] = None
        if "scope_checks" in packet:
            report["applicable_ids"] = []
        return {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(report)}]}]}

    def judge(self, name, payload, *, purpose):
        self.assertEqual(name, "opus")
        text = payload["messages"][0]["content"]
        self.opus_packets.append(json.loads(text))
        reply = {"model": opus.OPUS_MODEL, "stop_reason": "end_turn", "content": [{"type": "text", "text": self.xml}]}
        return opus.AuditedReply(reply, lambda _: None, request_id="a" * 32)

    def review(self, case):
        return opus.review(self.judge, case, observer=self.observations.append)

    def propose(self, call_id, note, *, reason="Append to the private reading log.", digest=None):
        return self.gate.dispatch(call_id, "workspace_write", {
            "path": self.path, "text": note, "expected_sha256": self.sha if digest is None else digest,
            "rationale": reason}, self.execute)

    def retire_fixture_watches(self):
        with database(self.gate.path) as db:
            # Keep the original case numbers when exercising the two separate origins.
            db.executemany("""INSERT INTO context_concerns(id,thread,sequence,evidence,summary)
                VALUES (?,?,1,?,?)""", [
                (9, self.thread, "Proposed a shell operation.", "Sandbox and draft publication."),
                (13, self.thread, "An earlier log draft copied a passage.", "Copying into the reading log."),
                (17, "other-room", "private evidence from another room", "private summary from another room")])
            concerns.record(db, self.thread, 9, 0, "watch", "Review shell operations.", ["coding_run"])
            concerns.record(db, self.thread, 9, 1, "retire", "Shell issue resolved by reviewer.", [])
            concerns.record(db, self.thread, 13, 0, "watch", "Review old note restrictions.", ["workspace_write"])
        self.store.complete(self.batch.id, "Synthetic fixture ready.")
        self.grant = "Allow original reactions in this private log. Omit passages copied from the source."
        self.command_id = self.store.submit("/release-concern 13 " + self.grant)
        self.batch = self.store.claim()
        self.gate.batch_id = self.batch.id

    def test_inherited_labels_and_authority_reach_both_reviewers_and_shadow(self):
        self.retire_fixture_watches()
        self.gate.observe({"input": "Update the private test log."})
        self.flag = "Could concern #9 v2 have restricted the reading log?"
        addition = "Page 3: I found this part moving.\n"
        candidate = self.original + addition
        result = self.propose("append-reaction", candidate, reason="Record a private reaction under concern #13.")
        self.assertIn("sha256", result)
        screen = self.luna_packets[-1]
        review, = self.opus_packets
        for packet in (screen, review):
            facts = packet["host_facts"]
            edit = facts["workspace_edit"]
            self.assertTrue(edit["complete"])
            self.assertEqual((edit["status"], edit["base_sha256"]), ("replace", self.sha))
            self.assertEqual([(h["old_offset"], h["new_offset"], h["removed"], h["added"]) for h in edit["hunks"]],
                             [(len(self.original), len(self.original), "", addition)])
            citation = next(c for c in facts["concern_citations"] if c["id"] == 13)
            self.assertEqual(citation["current"]["status"], "retired")
            self.assertEqual(citation["current"]["source"], "authenticated_operator")
            self.assertEqual(citation["operator_release"]["reason"], self.grant)
            self.assertEqual(citation["operator_release"]["command_event_id"], self.command_id)
        self.assertEqual(screen["arguments"]["text"], candidate)
        self.assertEqual(review["case"]["arguments"]["text"], candidate)
        self.assertNotIn(9, [c["id"] for c in screen["host_facts"]["concern_citations"]])
        cited = next(c for c in review["host_facts"]["concern_citations"] if c["id"] == 9)
        self.assertEqual(cited["screening_summary"], "Sandbox and draft publication.")
        self.assertEqual(cited["cited_version"], 2)
        self.assertEqual(cited["current"]["source"], "review_record")
        self.assertEqual(cited["current"]["last_active_tools"], ["coding_run"])
        self.assertNotIn("operator_release", cited)
        shadow, = self.observations
        self.assertEqual(json.loads(shadow["snapshot"]["case_text"]), review)
        self.assertEqual((self.files / self.path).read_bytes(), candidate.encode())

    def test_new_quotation_stays_visible_and_a_prior_denials_citations_are_checked_on_retry(self):
        self.retire_fixture_watches()
        self.gate.observe({"input": "Check the next entry."})
        addition = 'Copied passage: "This sentence is synthetic protected text."\n'
        candidate = self.original + addition
        self.xml = ('<review decision="deny">This adds a quotation. The claim attributed to concern #9 v2, '
                    'concern #9 v42, and concern #17 needs checking.</review>')
        first = self.propose("quoted", candidate)
        self.assertIn("denied", first["error"])
        self.assertEqual((self.files / self.path).read_bytes(), self.original.encode())
        initial_facts = self.opus_packets[0]["host_facts"]
        self.assertEqual("".join(h["added"] for h in initial_facts["workspace_edit"]["hunks"]), addition)
        self.assertFalse(initial_facts.get("concern_citations"))
        self.flag = ""
        self.xml = '<review decision="deny">The proposed addition still contains the protected quotation.</review>'
        retry = self.propose("retry-quoted", candidate, digest=self.sha[:12].upper())
        self.assertIn("denied", retry["error"])
        self.assertEqual(len(self.opus_packets), 2)
        for packet in (self.luna_packets[-1], self.opus_packets[-1]):
            facts = packet["host_facts"]
            self.assertIn("The claim attributed", facts["prior_exact_denial"])
            cited = {(c["id"], c.get("cited_version")): c for c in facts["concern_citations"]}
            self.assertEqual(cited[9, 2]["resolution"], "resolved")
            self.assertEqual(cited[9, 42]["resolution"], "unknown_version")
            self.assertEqual(cited[17, None]["resolution"], "unknown_concern")
            self.assertNotIn("private evidence from another room", json.dumps(packet))
            self.assertNotIn("private summary from another room", json.dumps(packet))
        self.assertEqual((self.files / self.path).read_bytes(), self.original.encode())

    def test_standalone_context_referral_receives_the_sources_behind_its_claim(self):
        self.retire_fixture_watches()
        self.context_flag = "The agent invoked concern #9 v2 to interpret concern #13."
        self.xml = '<concern id="18" decision="close_resolved">These cases concerned different topics.</concern>'
        self.gate.observe({"input": "Update the fixture reading log."})
        review, = self.opus_packets
        self.assertNotIn("tool", review["case"])
        citations = {item["id"]: item for item in review["host_facts"]["concern_citations"]}
        self.assertEqual(citations[9]["current"]["source"], "review_record")
        self.assertEqual(citations[13]["operator_release"]["reason"], self.grant)
        with database(self.gate.path) as db:
            self.assertEqual(concerns.get(db, self.thread, 18)["status"], "retired")

    def test_periodic_screen_checks_citations_first_raised_in_recent_conversation(self):
        self.gate.sample_every = 1
        self.gate.observe({"input": [{"role": "user", "content": "Do we have a record for concern #77?"}]})
        first, = self.luna_packets
        self.assertNotIn("concern_history", first)
        self.assertIn(CONCERN_SOURCE_POLICY, self.luna_instructions[-1])
        self.assertEqual(first["host_facts"]["concern_citations"], [{"id": 77, "resolution": "unknown_concern"}])

        self.retire_fixture_watches()
        self.gate.observe({"input": [{"role": "assistant", "content": "I thought concern #9 v1 imposed this note limit."},
                                    {"role": "user", "content": "Please check the scope against concern #13."}]})
        periodic = self.luna_packets[-1]
        self.assertEqual(len(self.luna_packets), 2)
        self.assertEqual(periodic["kind"], "context")
        self.assertIn(CONCERN_SOURCE_POLICY, self.luna_instructions[-1])
        citations = {item["id"]: item for item in periodic["host_facts"]["concern_citations"]}
        self.assertEqual(citations[9]["current"]["status"], "retired")
        self.assertEqual(citations[9]["cited_ruling"]["tools"], ["coding_run"])
        self.assertEqual(citations[9]["cited_ruling"]["source"], "review_record")
        self.assertEqual(citations[13]["operator_release"]["reason"], self.grant)
        self.assertEqual(self.opus_packets, [])

    def test_allow_after_concurrent_edit_still_uses_the_proposed_base_hash(self):
        self.gate.observe({"input": "Append a line."})
        self.xml = '<review decision="allow">Permit the tested edit.</review>'
        concurrent = "Another author changed this file while review was in flight.\n"

        def change_while_reviewing(case):
            self.execute("workspace_write", {"path": self.path, "text": concurrent, "expected_sha256": self.sha})
            return self.review(case)

        self.gate.review = change_while_reviewing
        self.execute = Mock(wraps=self.execute)
        result = self.propose("race", self.original + "An ordinary addition.\n", digest=self.sha[:12])
        self.assertIn("error", result)
        self.assertEqual((self.files / self.path).read_bytes(), concurrent.encode())
        review, = self.opus_packets
        self.assertEqual(review["host_facts"]["workspace_edit"]["base_sha256"], self.sha)
        self.assertEqual(self.execute.call_args.args[1]["expected_sha256"], self.sha)

    def test_partial_edits_show_exact_change_and_operator_receipts_without_resending_the_file(self):
        self.retire_fixture_watches()
        self.gate.observe({"input": "Revise this private note."})
        self.execute = Mock(wraps=self.execute)
        addition = "I enjoyed it."
        steps = [("workspace_append", {"text": addition, "expected_suffix": "label.\n"}, "", addition),
                 ("workspace_replace", {"find": addition, "replace": ""}, addition, "")]
        for index, (name, arguments, removed, added) in enumerate(steps):
            with self.subTest(name=name):
                args = {"path": self.path, **arguments}
                result = self.gate.dispatch(f"partial-{index}", name, {**args, "rationale": "Update my reading note."},
                                            self.execute)
                self.assertIn("sha256", result)
                screen, review = self.luna_packets[-1], self.opus_packets[-1]
                pinned = {**args, "expected_sha256": self.sha}
                self.assertEqual(self.execute.call_args.args, (name, pinned))
                self.assertEqual(screen["arguments"], pinned)
                self.assertEqual(review["case"]["arguments"], pinned)
                for packet in (screen, review):
                    facts = packet["host_facts"]
                    self.assertEqual(facts["concern_releases"][0]["concern_id"], 13)
                    edit = facts["workspace_edit"]
                    self.assertEqual((edit["base_sha256"], edit["proposed_sha256"]), (self.sha, result["sha256"]))
                    self.assertEqual([(h["removed"], h["added"]) for h in edit["hunks"]], [(removed, added)])
                    self.assertNotIn("earlier entry", json.dumps(packet))
                self.sha = result["sha256"]
        self.assertEqual((self.files / self.path).read_bytes(), self.original.encode())

    def test_partial_edits_without_digest_do_not_apply_to_a_newer_revision(self):
        self.gate.observe({"input": "Revise this private note."})
        self.execute = Mock(wraps=self.execute)
        path = self.files / self.path
        concurrent = self.original + "Saved by another writer.\n"

        def change_during_review(case):
            path.write_bytes(concurrent.encode())
            return self.review(case)

        self.gate.review = change_during_review
        for name, args in [("workspace_append", {"text": "Addition."}),
                           ("workspace_replace", {"find": "Page 2", "replace": "Second page"})]:
            with self.subTest(name=name):
                path.write_bytes(self.original.encode())
                result = self.gate.dispatch(name, name, {"path": self.path, **args, "rationale": "Revise."}, self.execute)
                self.assertIn("error", result)
                self.assertEqual(self.execute.call_args.args[1]["expected_sha256"], self.sha)
                self.assertEqual(path.read_bytes(), concurrent.encode())

    def test_failed_partial_precondition_cannot_become_valid_during_screening(self):
        self.gate.observe({"input": "Append a note after the marker."})
        screen = self.gate._screen
        concurrent = self.original + "Marker"

        def add_marker_during_screen(packet):
            (self.files / self.path).write_bytes(concurrent.encode())
            return screen(packet)

        self.gate._screen = add_marker_during_screen
        self.execute = Mock(wraps=self.execute)
        result = self.gate.dispatch("suffix", "workspace_append", {"path": self.path, "text": "Follow-up",
                                    "expected_suffix": "Marker", "rationale": "Add a note."}, self.execute)
        self.assertIn("expected_suffix", result["error"])
        self.execute.assert_not_called()
        self.assertEqual(self.opus_packets, [])

    def test_same_result_uses_the_prior_exact_denial_across_all_three_edit_tools(self):
        self.gate.observe({"input": "Try a note edit."})
        self.xml = '<review decision="deny">The change includes the restricted quotation.</review>'
        addition = "A synthetic quotation.\n"
        denied = self.propose("full-denial", self.original + addition)
        self.assertIn("denied", denied["error"])
        self.flag = ""
        for name, args in [("workspace_append", {"text": addition, "expected_suffix": "label.\n"}),
                           ("workspace_replace", {"find": "label.\n", "replace": "label.\n" + addition})]:
            with self.subTest(name=name):
                result = self.gate.dispatch(name, name, {"path": self.path, **args, "rationale": "Revise."}, self.execute)
                self.assertIn("denied", result["error"])
                for packet in (self.luna_packets[-1], self.opus_packets[-1]):
                    self.assertIn("restricted quotation", packet["host_facts"]["prior_exact_denial"])
        self.assertEqual(len(self.opus_packets), 3)
        self.assertEqual((self.files / self.path).read_bytes(), self.original.encode())

    def test_whole_write_watch_and_pending_operator_question_apply_to_partial_edits(self):
        self.gate.observe({"input": "Revise this private note."})
        with database(self.gate.path) as db:
            db.executemany("INSERT INTO context_concerns(id,thread,sequence,evidence,summary) VALUES (?,?,1,?,?)",
                           [(20, self.thread, "A file edit.", "Existing write watch."),
                            (21, self.thread, "An append.", "Append-only watch.")])
            concerns.record(db, self.thread, 20, 0, "watch", "Review file changes.", ["workspace_write"])
            concerns.record(db, self.thread, 21, 0, "watch", "Review appends.", ["workspace_append"])
        question = self.room.open(self.thread, f"workspace_write:{self.path}", "workspace_write", "prior",
                                  "Edit note", "", "Which passages should the note retain?")
        self.execute = Mock(wraps=self.execute)
        for name, args, ids in [("workspace_append", {"text": "Addition."}, [20, 21]),
                                ("workspace_replace", {"find": "Page 2", "replace": "Second page"}, [20])]:
            with self.subTest(name=name):
                result = self.gate.dispatch(name, name, {"path": self.path, **args, "rationale": "Revise."}, self.execute)
                self.assertEqual(result["review_id"], question)
                self.assertEqual([row["id"] for row in self.luna_packets[-1]["scope_checks"]], ids)
        self.assertEqual(self.opus_packets, [])
        self.execute.assert_not_called()

    def test_unresolved_prefix_cannot_run_if_the_matching_file_appears_during_screening(self):
        self.gate.observe({"input": "Update the note."})
        (self.files / self.path).unlink()
        screen = self.gate._screen

        def restore_while_screening(packet):
            (self.files / self.path).write_bytes(self.original.encode())
            return screen(packet)

        self.gate._screen = restore_while_screening
        self.execute = Mock(wraps=self.execute)
        result = self.propose("stale", "Replacement", digest=self.sha[:12])
        self.assertIn("error", result)
        self.execute.assert_not_called()

    def test_large_packet_preserves_the_full_candidate_and_marks_omitted_edit_excerpts(self):
        self.gate.observe({"input": "Append to a large fixture."})
        base = "\u2605" * 20_000 + "\n"
        self.sha = self.execute("workspace_write", {"path": self.path, "text": base,
                                                     "expected_sha256": self.sha})["sha256"]
        addition = "x" * 3_000
        candidate = base + addition
        result = self.propose("large-edit", candidate)
        self.assertIn("sha256", result)
        self.assertEqual(self.luna_packets[-1]["host_facts"]["workspace_edit"]["hunks"][0]["added"], addition)
        review, = self.opus_packets
        self.assertEqual(review["case"]["arguments"]["text"], candidate)
        edit = review["host_facts"]["workspace_edit"]
        self.assertFalse(edit["complete"])
        self.assertEqual(edit["hunks"], [])
        self.assertIn("limit", edit["reason"])
        self.assertEqual(edit["base_sha256"], self.sha)
        self.assertEqual(json.loads(self.observations[0]["snapshot"]["case_text"]), review)
