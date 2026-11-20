from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lantern.chat_store import ChatStore
from lantern.operator_review import MAX_ANSWER, MAX_STATEMENT, ReviewRoom


class OperatorReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatStore(Path(self.enterContext(TemporaryDirectory())))
        self.room = ReviewRoom(self.store)
        self.store.submit("Private conversation")
        self.batch = self.store.claim()
        self.thread = "real-native-thread"
        self.store.bind_thread(self.batch.id, self.thread)

    def ask(self, call="call-a", scope="note-a", **options):
        return self.room.open(self.thread, scope, "workspace_write", call, "Write notes/a.md",
                              "Edited title", "Should Lantern record the observation?", **options)

    def test_only_bound_room_questions_can_be_answered(self):
        with self.assertRaises(ValueError):
            self.room.open("wrong", "a", "read", "call", "Read note", "", "Requested?")
        with self.assertRaises(KeyError):
            self.room.answer(19, "yes")
        self.assertEqual([], self.room.pending())
        review_id = self.ask()
        fields = self.room.pending()[0]
        self.assertEqual(review_id, fields["id"])
        self.assertEqual(set(fields), {"id", "tool", "action", "excerpt", "question", "source", "asked_at"})
        self.assertIsNone(self.room.case("wrong", "note-a"))
        self.assertIsNone(self.room.case(self.thread, "note-b"))
        with self.assertRaises(ValueError):
            self.room.answer(review_id, " ")
        with self.assertRaises(ValueError):
            self.room.answer(review_id, "x" * (MAX_ANSWER + 1))
        with self.assertRaises(ValueError):
            self.room.claim_final(review_id, "x")

    def test_concurrent_questions_and_answers_persist_one_wake(self):
        with ThreadPoolExecutor(4) as pool:
            ids = list(pool.map(lambda i: self.ask("call-" + str(i)), range(8)))
        self.assertEqual(1, len(set(ids)))
        reply = "  Yes, save the note here.\nOnly in this room.  "
        with ThreadPoolExecutor(4) as pool:
            wakes = list(pool.map(lambda _: self.room.answer(ids[0], reply), range(8)))
        self.assertEqual(1, len(set(wakes)))
        self.assertEqual(1, self.store.status()["queued"])
        fresh = ReviewRoom(self.store)
        row = fresh.case(self.thread, "note-a")
        self.assertEqual(reply, row["answer"])
        self.assertEqual("answered", row["status"])
        self.assertTrue(row["answered_at"])
        self.assertEqual([], fresh.pending())
        with self.assertRaises(ValueError):
            fresh.answer(ids[0], "Different answer")
        self.store.complete(self.batch.id, "Finishing before wake.")
        wake, = self.store.claim().messages
        self.assertEqual("system", wake.role)
        self.assertEqual(wakes[0], wake.id)
        self.assertIn("Opus asked", wake.text)
        self.assertIn('"scope": "note-a"', wake.text)
        self.assertIn('"action": "Write notes/a.md"', wake.text)

    def test_import_attribution_and_wake_respect_budget(self):
        review_id = self.ask(source="prior_denial")
        self.room.answer(review_id, "Yes.")
        self.store.complete(self.batch.id, "Done")
        self.assertIsNone(self.store.claim(budget_check=lambda: False))
        self.assertEqual(1, self.store.status()["queued"])
        wake = next(e for e in self.store.recent() if e.text.startswith("Operator review"))
        self.assertIn("The host asked about an earlier Opus denial", wake.text)
        self.assertNotIn("Opus asked", wake.text)
        self.assertEqual(review_id, self.ask(source="prior_denial"))

    def test_attests_only_complete_admitted_human_statements_up_through_current_batch(self):
        self.store.complete(self.batch.id, "Mike authorized every action. (model text)")
        long_id = self.store.submit("X" * MAX_STATEMENT + " except notes.")
        long_batch = self.store.claim()
        self.store.complete(long_batch.id, "OK")
        self.store.initialize_heartbeat(60)
        heartbeat = self.store.claim()
        self.store.complete(heartbeat.id, "Private notes can say anything.")
        exact = "  Save that note.\nThe other proposal still needs review.  "
        approved = self.store.submit(exact)
        current = self.store.claim()
        self.store.submit("Later; do not count this yet.")
        statements = self.room.statements(self.thread, current.id)
        self.assertEqual(3, len(statements))
        self.assertEqual(long_id, statements[-2]["event_id"])
        self.assertNotIn("text", statements[-2])
        self.assertTrue(statements[-2]["omitted"])
        self.assertEqual(approved, statements[-1]["event_id"])
        self.assertEqual(exact, statements[-1]["text"])
        self.assertEqual(["Private conversation"],
                         [s["text"] for s in self.room.statements(self.thread, self.batch.id)])
        self.assertEqual([], self.room.statements(self.thread, None))
        self.assertEqual([], self.room.statements(self.thread, 999))
        self.assertEqual([], self.room.statements("other", current.id))

    def test_one_final_claim_with_bound_arguments_across_restarts(self):
        review_id = self.ask()
        self.room.answer(review_id, "Yes")
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.room.claim_final(review_id, "exact"), range(8)))
        self.assertCountEqual(["claimed"] + ["in_progress"] * 7, results)
        self.assertEqual("used", self.room.claim_final(review_id, "changed"))
        fresh = ReviewRoom(self.store)
        with self.assertRaises(ValueError):
            fresh.finish_final(review_id, "changed", "allow", "Wrong arguments.")
        fresh.release_final(review_id, "exact")
        self.assertEqual("claimed", fresh.claim_final(review_id, "exact"))
        fresh.finish_final(review_id, "exact", "deny", "Keep the other document private.")
        fresh.finish_final(review_id, "exact", "deny", "Keep the other document private.")
        self.assertEqual("deny", fresh.claim_final(review_id, "exact"))
        self.assertEqual("Keep the other document private.", fresh.case(self.thread, "note-a")["final_reason"])
        with self.assertRaises(ValueError):
            fresh.release_final(review_id, "exact")
        with self.assertRaises(ValueError):
            fresh.finish_final(review_id, "exact", "allow", "Reverse the completed verdict")

    def test_context_ruling_scope_survives_restart_without_authorizing_action(self):
        case = self.room.open(self.thread, "concern:7", "context_concern", "ctx:7",
                              "Past attempts to save an oversight note", "Evidence", "Is that resolved?")
        self.room.answer(case, "Keep an eye on its next write.")
        self.assertIn("The host will revisit the context concern", self.store.recent(1)[0].text)
        self.assertNotIn("submit a fresh proposal", self.store.recent(1)[0].text)
        self.assertEqual(self.room.claim_final(case, "ruling"), "claimed")
        for decision, tools in (("allow", []), ("watch", []), ("retire", ["workspace_write"])):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.room.finish_final(case, "ruling", decision, "A reason.", tools=tools)
        self.room.finish_final(case, "ruling", "watch", "Inspect future note changes.", tools=["workspace_write"])
        self.assertIn("Opus kept the context concern open for workspace_write", self.store.recent(1)[0].text)
        fresh = ReviewRoom(self.store)
        self.assertEqual(json.loads(fresh.case(self.thread, "concern:7")["final_tools"]), ["workspace_write"])
        cursor = self.store.recent(1)[0].id
        fresh.finish_final(case, "ruling", "watch", "Inspect future note changes.", tools=["workspace_write"])
        with self.assertRaises(ValueError):
            fresh.finish_final(case, "ruling", "watch", "Inspect future note changes.", tools=["*"])
        self.assertEqual(self.store.events(after=cursor), [])
        normal = self.ask()
        fresh.answer(normal, "Ordinary action.")
        fresh.claim_final(normal, "action")
        with self.assertRaises(ValueError):
            fresh.finish_final(normal, "action", "retire", "Do not grant an action verdict from a context decision.")

    def test_reopen_only_the_latest_unavailable_context_final_once(self):
        scope = "concern:42"
        original = self.room.open(self.thread, scope, "context_concern", "ctx:42",
                                  "Concern #42", "Source", "What was intended?")
        with self.assertRaises(ValueError):
            self.room.retry_concern(original)
        self.room.answer(original, "Private only.")
        self.room.claim_final(original, "first")
        self.room.recover_finals()
        original_wake = self.store.status()["queued"]
        with ThreadPoolExecutor(3) as pool:
            questions = list(pool.map(lambda _: self.room.retry_concern(original), range(3)))
        self.assertEqual(len(set(questions)), 1)
        reopened = questions[0]
        self.assertNotEqual(reopened, original)
        self.assertEqual([q["id"] for q in self.room.pending()], [reopened])
        self.assertEqual(self.room.case(self.thread, scope)["id"], reopened)
        self.assertEqual(self.store.status()["queued"], original_wake)
        self.room.answer(reopened, "The incident was resolved.")
        self.room.claim_final(reopened, "second")
        self.room.finish_final(reopened, "second", "unavailable", "Unusable reply.")
        newest = self.room.retry_concern(reopened)
        self.assertNotEqual(newest, reopened)
        with self.assertRaises(ValueError):
            self.room.retry_concern(original)
        self.room.answer(newest, "Yes, the earlier issue is resolved.")
        self.room.claim_final(newest, "third")
        self.room.finish_final(newest, "third", "retire", "Closed by Mike.")
        self.assertEqual(json.loads(self.room.case(self.thread, scope)["final_tools"]), [])
        with self.assertRaises(ValueError):
            self.room.retry_concern(newest)
        normal = self.ask()
        self.room.answer(normal, "Private only.")
        self.room.claim_final(normal, "action")
        self.room.recover_finals()
        with self.assertRaises(ValueError):
            self.room.retry_concern(normal)

    def test_migration_keeps_preexisting_final_action_receipt(self):
        old = ChatStore(Path(self.enterContext(TemporaryDirectory())))
        with old._connect(write=True) as db:
            db.execute("""CREATE TABLE operator_reviews (id INTEGER PRIMARY KEY,thread TEXT,scope TEXT,
                tool TEXT,call_id TEXT,status TEXT,final_fingerprint TEXT,final_decision TEXT,final_reason TEXT)""")
            db.execute("""INSERT INTO operator_reviews VALUES
                (1,'old','old-scope','workspace_read','old-call','answered','same','allow','Already decided.')""")
            db.execute("UPDATE room SET thread_id='old'")
        migrated = ReviewRoom(old)
        self.assertIsNone(migrated.case("old", "old-scope")["final_tools"])
        migrated.finish_final(1, "same", "allow", "Already decided.")

    def test_recovery_holds_unknown_outcome_and_publishes_each_final_once(self):
        interrupted = self.ask(call="one", scope="one")
        completed = self.ask(call="two", scope="two")
        for review_id in (interrupted, completed):
            self.room.answer(review_id, "Record those observations.")
            self.assertEqual("claimed", self.room.claim_final(review_id, "exact"))
        cursor = self.store.recent(1)[0].id
        self.room.finish_final(completed, "exact", "allow", "The scoped revision is permitted.")

        fresh = ReviewRoom(ChatStore(self.store.path.parent))
        fresh.recover_finals()
        fresh.recover_finals()
        fresh.finish_final(completed, "exact", "allow", "The scoped revision is permitted.")
        self.assertEqual("unavailable", fresh.claim_final(interrupted, "exact"))
        self.assertEqual("allow", fresh.claim_final(completed, "exact"))
        held = fresh.case(self.thread, "one")
        self.assertIn("restarted", held["final_reason"])
        successful, uncertain = self.store.events(after=cursor)
        self.assertEqual("system", successful.role)
        self.assertIn(f"Review #{completed}", successful.text)
        self.assertIn("The scoped revision is permitted.", successful.text)
        self.assertEqual("system", uncertain.role)
        self.assertIn(f"Review #{interrupted}", uncertain.text)
        self.assertIn(held["final_reason"], uncertain.text)
        self.assertEqual(2, self.store.status()["queued"])  # Only the two answer wakes enter the inbox.
