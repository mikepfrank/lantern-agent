import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from lantern import concerns
from lantern.chat_store import ChatStore
from lantern.operator_review import ReviewRoom


class OperatorReleaseTests(unittest.TestCase):
    def setUp(self):
        state = Path(self.enterContext(TemporaryDirectory()))
        self.store = ChatStore(state)
        self.room = ReviewRoom(self.store)
        self.ledger = self.enterContext(closing(sqlite3.connect(state / "supervision.sqlite")))
        self.thread = "room-thread"
        self.source = self.store.submit("Concern #1 can end. The earlier disposition stands.")
        batch = self.store.claim()
        self.store.bind_thread(batch.id, self.thread)
        self.store.complete(batch.id, "Received")
        with self.ledger as db:
            concerns.initialize(db)
            for sequence in range(1, 3):
                concern = concerns.create(db, self.thread, sequence, "Evidence", "Summary")
                concerns.record(db, self.thread, concern, 0, "watch", "Watch.", ["workspace_read"])
            concerns.create(db, "other", 1, "Private", "Other room's concern")
            db.execute("CREATE TABLE action_refusals (reason TEXT)")
            db.execute("INSERT INTO action_refusals VALUES ('exact denial')")

    def latest(self, concern_id):
        return concerns.get(self.ledger, self.thread, concern_id)

    def test_admitted_operator_command_releases_only_named_concern_and_closes_its_question(self):
        review = self.room.open(self.thread, "context-concern:1", "context_concern", "one", "Concern", "", "End watch?")
        other = self.room.open(self.thread, "context-concern:2", "context_concern", "two", "Other", "", "Continue?")
        command = f"/release-concern 1 --cite {self.source}"
        command_id = self.store.submit(command, "web:retry-safe")
        batch = self.store.claim()
        self.assertEqual([event.role for event in batch.messages], ["system"])
        self.assertIn("Mike retired context concern #1", batch.messages[0].text)
        self.assertEqual(self.latest(1)["status"], "retired")
        self.assertEqual(self.latest(2)["status"], "watch")
        self.assertEqual(self.ledger.execute("SELECT reason FROM action_refusals").fetchone(), ("exact denial",))
        self.assertEqual([q["id"] for q in self.room.pending()], [other])
        self.assertEqual(self.room.case(self.thread, "context-concern:1")["id"], review)
        self.assertEqual(self.room.case(self.thread, "context-concern:1")["final_decision"], "retire")
        release, = self.room.concern_releases(self.thread, "workspace_read")
        self.assertEqual(release["source"], "authenticated_operator")
        self.assertEqual(release["operator_action"], "retired")
        self.assertEqual((release["reason_source"], release["operator_command"]), ("host_receipt", command))
        self.assertEqual(release["cited_operator_statement"], "Concern #1 can end. The earlier disposition stands.")
        self.assertEqual(release["prior_tools"], ["workspace_read"])
        item, = concerns.citation_receipts(self.ledger, self.thread, [(1, 1)], self.room)
        self.assertEqual(item["current"]["source"], "authenticated_operator")
        self.assertEqual(item["cited_ruling"]["source"], "review_record")
        self.assertEqual(item["operator_release"], release)
        self.assertEqual((release["command_event_id"], release["source_event_id"], release["receipt_event_id"]),
                         (command_id, self.source, batch.messages[0].id))
        self.assertEqual(self.room.concern_releases(self.thread, "workspace_write"), [])
        self.assertEqual(self.room.concern_releases(self.thread, "consult_opus"), [release])
        self.assertEqual(self.room.concern_releases("other", "workspace_read"), [])
        self.assertEqual(self.store.submit(command, "web:retry-safe"), command_id)
        self.store.submit("/release-concern 1")
        self.assertEqual(self.store.claim_steer(batch.id), ())
        self.assertIn("already retired", self.store.recent(1)[0].text)
        self.assertEqual(self.latest(1)["version"], 2)
        with self.assertRaisesRegex(ValueError, "already retired"):
            self.room.open(self.thread, "context-concern:1", "context_concern", "late", "Stale", "", "Stay?")
        with self.ledger as db:
            self.assertFalse(concerns.record(db, self.thread, 1, 1, "watch", "Stale review", ["*"]))

    def test_malformed_cross_room_or_untrusted_references_cannot_release(self):
        for command in ("/release-concern 3", "/release-concern 2 --cite 1",
                        "/release-concern 1 --cite 2", "/release-concern 1\n/release-concern 2"):
            self.store.submit(command)
            self.assertIsNone(self.store.claim())
            self.assertIn("declined", self.store.recent(1)[0].text)
        self.store.submit("Lantern said: /release-concern 1")
        batch = self.store.claim()
        self.store.notify(batch.id, "/release-concern 1", "agent-produced")
        self.assertEqual(self.store.claim_steer(batch.id), ())
        self.assertEqual(self.latest(1)["status"], "watch")
        self.assertEqual(self.latest(2)["status"], "watch")

    def test_first_operator_confirmation_of_reviewer_retirement_keeps_ruling_and_last_scope(self):
        with self.ledger as db:
            concerns.record(db, self.thread, 1, 1, "watch", "Narrowed watch.", ["workspace_write"])
            concerns.record(db, self.thread, 1, 2, "retire", "Reviewer ended the incident.", [])
        original = self.latest(1)
        command = f"/release-concern 1 --cite {self.source}"
        command_id = self.store.submit(command, "first-confirmation")
        self.assertIsNone(self.store.claim(budget_check=lambda: self.fail("unexpected wake")))
        receipt = self.store.recent(1)[0]
        self.assertIn("confirmed the retirement of context concern #1", receipt.text)
        self.assertEqual(receipt.role, "system")
        self.assertEqual(self.latest(1), original)
        self.assertEqual(self.store.status()["queued"], 0)
        evidence, = self.room.concern_releases(self.thread, "workspace_write")
        self.assertIn("existing retirement at ruling version 3", evidence["reason"])
        item, = concerns.citation_receipts(self.ledger, self.thread, [(1, 3)], self.room)
        self.assertEqual(item["operator_release"]["operator_action"], "confirmed_retirement")
        self.assertEqual(item["cited_ruling"]["source"], "review_record")
        self.assertEqual(item["cited_ruling"]["reason"], "Reviewer ended the incident.")
        self.assertEqual((evidence["command_event_id"], evidence["source_event_id"], evidence["receipt_event_id"]),
                         (command_id, self.source, receipt.id))
        self.assertEqual(self.room.concern_releases(self.thread, "workspace_read"), [])
        self.assertEqual(self.room.concern_releases(self.thread, "consult_opus"), [evidence])
        self.assertEqual(self.store.submit(command, "first-confirmation"), command_id)
        self.store.submit(command)
        self.assertIsNone(self.store.claim(budget_check=lambda: self.fail("unexpected wake")))
        self.assertEqual(self.room.concern_releases(self.thread, "workspace_write"), [evidence])
        self.assertIn(f"receipt #{receipt.id} is on record", self.store.recent(1)[0].text)
        self.assertEqual(self.latest(1), original)

    def test_immediately_retired_finding_supplies_confirmation_only_to_consultations(self):
        with self.ledger as db:
            concern = concerns.create(db, self.thread, 3, "Reported", "No watch issued")
            concerns.record(db, self.thread, concern, 0, "retire", "No issue.", [])
        self.store.submit(f"/release-concern {concern}")
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.room.concern_releases(self.thread, "workspace_write"), [])
        confirmation, = self.room.concern_releases(self.thread, "consult_opus")
        self.assertEqual(confirmation["concern_id"], concern)
        self.assertEqual(self.latest(concern)["version"], 1)

    def test_release_during_active_budget_pause_preserves_prior_answer(self):
        review = self.room.open(self.thread, "context-concern:1", "context_concern", "one", "Concern", "", "End?")
        answer_id = self.room.answer(review, "Please explain.")
        self.assertEqual(self.room.claim_final(review, "inflight"), "claimed")
        with self.store._connect(write=True) as db:
            db.execute("UPDATE scheduler SET budget_until='9999-01-01T00:00:00+00:00'")
        self.store.submit("/release-concern #1 This incident is resolved.")
        self.assertIsNone(self.store.claim(budget_check=lambda: self.fail("model gate called")))
        self.assertEqual(self.latest(1)["status"], "retired")
        self.room.finish_final(review, "inflight", "watch", "Late response", tools=["*"])
        self.room.release_final(review, "inflight")
        row = self.room.case(self.thread, "context-concern:1")
        self.assertEqual((row["answer"], row["wake_id"], row["final_decision"]), ("Please explain.", answer_id, "retire"))
        self.assertIn("This incident is resolved", self.store.recent(1)[0].text)

    def test_receipt_failure_rolls_back_ledger_and_command_can_resume(self):
        self.store.submit("/release-concern 1")
        with self.store._connect(write=True) as db:
            db.execute("""CREATE TRIGGER interrupt_receipt BEFORE INSERT ON operator_concern_releases
                BEGIN SELECT RAISE(ABORT,'test interruption'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test interruption"):
            self.store.claim()
        self.assertEqual(self.latest(1)["status"], "watch")
        self.assertEqual(self.store.status()["queued"], 1)
        self.assertEqual(self.store.recent(1)[0].role, "operator")
        with self.store._connect(write=True) as db:
            db.execute("DROP TRIGGER interrupt_receipt")
        after_restart = ChatStore(self.store.path.parent)
        receipt, = after_restart.claim().messages
        self.assertIn("retired context concern #1", receipt.text)
        self.assertEqual(self.latest(1)["version"], 2)
