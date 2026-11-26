import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.clone_grants import CloneGrants, checkout_path
from lantern.repository_clones import clone_tool


FIRST, SECOND, THIRD = (letter * 40 for letter in "abc")


class RepositoryApprovalTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.state = Path(directory)
        self.grants = CloneGrants(self.state)
        self.spool = self.state / "packs"
        self.spool.mkdir()
        self.enterContext(patch("lantern.repository_clones.PACKS", self.spool))
        self.resolve = self.enterContext(patch("lantern.clone_grants.resolve_branch"))
        self.resolve.return_value = {"branch": "main", "repository_id": 17, "commit": FIRST}
        self.fetch = self.enterContext(patch("lantern.repository_clones.fetch_pack", side_effect=self.write_pack))
        self.client = self.enterContext(patch("lantern.repository_clones.deliver_clone", side_effect=self.receive))

    @staticmethod
    def write_pack(repository, tip, path, *, repository_id):
        data = f"{repository_id} {repository} {tip}".encode()
        path.write_bytes(data)
        return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    @staticmethod
    def receive(socket, repository, tip, sha256):
        return {"status": "cloned", "path": checkout_path(repository, tip)}

    def request(self, name="astronomy-team/geometry"):
        return clone_tool(self.grants, None, "request_repository_clone",
                          {"repository": name, "purpose": "Inspect the derivation used in this model."}, "request")

    def clone(self, number, key):
        args = {"grant_id": number}
        reviewed = self.grants.evidence(args, key)
        return clone_tool(self.grants, None, "clone_repository",
                          {**args, "target_commit": reviewed["target_commit"]}, key)

    def test_agent_proposes_independent_repositories_and_cannot_approve(self):
        first = self.request()
        self.resolve.return_value = {"branch": "lab", "repository_id": 61, "commit": SECOND}
        other = self.request("someone-else/measurements")
        self.assertEqual({r["id"] for r in self.grants.overview()["requests"]}, {first["id"], other["id"]})
        self.assertEqual(other["branch"], "lab")
        with self.assertRaisesRegex(ValueError, "approved"):
            self.clone(first["id"], "trial")
        with self.assertRaisesRegex(ValueError, "only"):
            self.grants.evidence({"grant_id": first["id"], "target_commit": THIRD}, "trial")
        self.assertIn("error", clone_tool(self.grants, None, "allow_once", {"id": first["id"]}, "trial"))
        self.fetch.assert_not_called()

    def test_omitted_and_empty_branch_propose_the_same_resolved_revision(self):
        repo = "astronomy-team/geometry"
        self.resolve.return_value["branch"] = "trunk"
        omitted = self.request(repo)
        self.resolve.assert_called_once_with(repo, None)
        args = {"repository": repo, "purpose": "Read the observations.", "branch": ""}
        empty = clone_tool(self.grants, None, "request_repository_clone", args, "next")
        self.resolve.assert_called_with(repo, None)
        self.assertEqual((empty["id"], empty["branch"], empty["commit"], empty["status"]),
                         (omitted["id"], "trunk", FIRST, "pending"))
        self.resolve.return_value["branch"] = "default"
        explicit = clone_tool(self.grants, None, "request_repository_clone", {**args, "branch": "default"}, "new")
        self.resolve.assert_called_with(repo, "default")
        self.assertEqual(explicit["branch"], "default")
        self.assertNotEqual(explicit["id"], omitted["id"])
        self.fetch.assert_not_called()

    def test_operator_decision_and_original_pin_survive_restarts(self):
        req = self.request()
        self.resolve.return_value["commit"] = SECOND
        self.assertEqual(self.request()["id"], req["id"])
        self.grants.decide(req["id"], "deny")
        self.assertEqual(self.request()["status"], "denied")
        with self.assertRaisesRegex(ValueError, "approved"):
            self.clone(req["id"], "attempt")
        reopened = self.grants.reopen(req["id"])
        self.assertEqual(reopened["commit"], FIRST)
        self.grants.decide(reopened["id"], "allow_once")
        self.grants = CloneGrants(self.state)
        result = self.clone(reopened["id"], "retry")
        self.assertEqual((result["commit"], result["status"]), (FIRST, "cloned"))
        self.assertEqual(self.clone(reopened["id"], "another")["status"], "previously_delivered")
        self.fetch.assert_called_once()
        self.assertEqual(self.grants.status(reopened["id"])["status"], "fulfilled")
        self.assertEqual(self.grants.overview()["receipts"][0]["path"], result["path"])
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_standing_branch_update_pins_exact_reviewed_tip_and_checks_identity(self):
        number = self.request()["id"]
        self.grants.decide(number, "allow_standing")
        self.resolve.return_value["commit"] = SECOND
        first = self.clone(number, "first")
        reviewed = self.grants.evidence({"grant_id": number}, "second")
        self.resolve.return_value["commit"] = THIRD
        second = clone_tool(self.grants, None, "clone_repository",
                            {"grant_id": number, "target_commit": reviewed["target_commit"]}, "second")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual((first["commit"], second["commit"], second["previous_commit"]), (FIRST, SECOND, FIRST))
        self.resolve.return_value["commit"] = SECOND
        self.assertEqual(self.clone(number, "unchanged")["status"], "previously_delivered")
        self.resolve.return_value["commit"] = THIRD
        self.assertEqual(self.clone(number, "unchanged")["commit"], SECOND)
        self.assertEqual(len(self.grants.status(number)["receipts"]), 2)
        self.resolve.return_value["repository_id"] = 18
        with self.assertRaisesRegex(ValueError, "identity"):
            self.clone(number, "third")
        self.grants.revoke(number)
        with self.assertRaisesRegex(ValueError, "approved"):
            self.clone(number, "retry-revoked")
        self.assertEqual(self.fetch.call_count, 2)

    def test_lost_response_keeps_exact_revision_and_reuses_staging(self):
        number = self.request()["id"]
        self.grants.decide(number, "allow_standing")
        self.clone(number, "first")
        self.resolve.return_value["commit"] = SECOND
        self.client.side_effect = lambda *_: {"error": "reply lost"}
        self.assertEqual(self.clone(number, "uncertain"), {"error": "reply lost"})
        self.assertEqual(len(list(self.spool.iterdir())), 1)
        self.resolve.return_value["commit"] = THIRD
        self.client.side_effect = self.receive
        result = self.clone(number, "recovery")
        self.assertEqual((result["commit"], result["previous_commit"]), (SECOND, FIRST))
        self.assertEqual(self.clone(number, "uncertain")["status"], "previously_delivered")
        self.assertEqual(len(self.grants.status(number)["receipts"]), 2)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_revocation_during_fetch_and_definitive_failure(self):
        number = self.request()["id"]
        self.grants.decide(number, "allow_once")

        def revoke_while_in_flight(*args, **kwargs):
            self.grants.revoke(number)
            return self.write_pack(*args, **kwargs)

        self.fetch.side_effect = revoke_while_in_flight
        self.assertIn("approved", self.clone(number, "in-flight")["error"])
        self.client.assert_not_called()
        self.assertEqual(self.grants.overview()["receipts"], [])
        self.fetch.side_effect = self.write_pack
        new = self.grants.reopen(number)["id"]
        self.grants.decide(new, "allow_once")
        self.client.side_effect = lambda *_: {"error": "could not check out", "definitive": True}
        self.assertIn("error", self.clone(new, "bad-pack"))
        self.assertEqual(self.grants.status(new)["status"], "approved")
        self.assertEqual(list(self.spool.iterdir()), [])
        self.client.side_effect = self.receive
        self.assertEqual(self.clone(new, "fresh")["status"], "cloned")
