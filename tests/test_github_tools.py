import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from lantern.github_tools import OPEN_DRAFT, draft_evidence, github_tool
from lantern.supervision import Supervisor


def sample_proposal():
    proposal = {"base": "1" * 40, "source_commit": "2" * 40, "base_branch": "main",
                "repository": "mikepfrank/lantern-agent", "title": "Fix a test", "body": "For review.",
                "patch": "diff --git a/tests/check.py b/tests/check.py\n@@ -1 +1 @@\n-old\n+new\n",
                "paths": ["tests/check.py"]}
    canonical = json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode("ascii")
    return {**proposal, "proposal_id": hashlib.sha256(canonical).hexdigest()}


class GithubToolsTests(unittest.TestCase):
    def test_preview_and_evidence_require_the_complete_document_for_that_commit_or_id(self):
        original = sample_proposal()
        with patch("lantern.github_tools._call", return_value=original) as broker:
            self.assertEqual(github_tool("sock", "github_preview", {"commit": original["source_commit"]}), original)
            self.assertEqual(draft_evidence("sock", {"proposal_id": original["proposal_id"]}), original)
            self.assertIn("error", github_tool("sock", "github_preview", {"commit": "a" * 40}))
            for field in ("title", "body", "repository", "base_branch", "patch", "source_commit"):
                with self.subTest(field=field):
                    broker.return_value = {**original, field: "changed"}
                    with self.assertRaises(ValueError):
                        draft_evidence("sock", {"proposal_id": original["proposal_id"]})
            broker.return_value = {k: v for k, v in original.items() if k != "patch"}
            with self.assertRaises(ValueError):
                draft_evidence("sock", {"proposal_id": original["proposal_id"]})

    def test_registry_arguments_and_lost_publication_receipt(self):
        with patch("lantern.github_tools.socket.socket") as make, \
                patch("lantern.github_tools.socket.AF_UNIX", 1, create=True):
            for name, args in [("github_sync", {"url": "elsewhere"}), ("github_preview", {"commit": "main"}),
                               (OPEN_DRAFT, {"proposal_id": "1" * 64, "patch": "other"})]:
                with self.assertRaises(ValueError):
                    github_tool("sock", name, args)
            make.assert_not_called()
            peer = make.return_value.__enter__.return_value
            peer.recv.return_value = b""
            reply = github_tool("sock", OPEN_DRAFT, {"proposal_id": "f" * 64})
            self.assertIn("unknown", reply["error"])
            self.assertEqual(json.loads(peer.sendall.call_args.args[0]), {"op": "open_draft", "proposal_id": "f" * 64})
            peer.recv.side_effect = [b'{"upstream":"', b'main"}\n']
            self.assertEqual(github_tool("sock", "github_sync", {}), {"upstream": "main"})
            self.assertEqual(json.loads(peer.sendall.call_args.args[0]), {"op": "sync"})

    def test_reviewers_receive_the_frozen_public_content_and_denial_prevents_dispatch(self):
        proposal = sample_proposal()
        fetch, luna, judge, execute = Mock(return_value=proposal), Mock(), Mock(), Mock()
        luna.screen.return_value = {"escalate": False, "summary": "A local checkout.", "evidence": ""}
        with TemporaryDirectory() as tmp:
            session = Supervisor(Path(tmp), luna, judge, Mock(), tool_names=[OPEN_DRAFT], draft_evidence=fetch)
            session.bind("fixture")
            session.observe({"input": "Propose the change.", "tools": []})
            luna.screen.return_value = {"escalate": True, "summary": "Public proposal.", "evidence": "Check outgoing data."}
            judge.return_value = {"decision": "deny", "reason": "This fixture is private."}
            args = {"proposal_id": proposal["proposal_id"], "rationale": "Share the patch for review."}
            failed = session.dispatch("pr", OPEN_DRAFT, args, execute)
            self.assertEqual(failed["judgment"], "This fixture is private.")
            self.assertEqual(luna.screen.call_args.args[0]["host_facts"]["public_draft"], proposal)
            self.assertEqual(judge.call_args.args[0]["host_facts"]["public_draft"], proposal)
            self.assertEqual(session.dispatch("pr", OPEN_DRAFT, args, execute), failed)
            fetch.assert_called_once_with({"proposal_id": proposal["proposal_id"]})
            execute.assert_not_called()
            fetch.side_effect = ValueError("Unavailable")
            luna.screen.return_value = {"escalate": False, "summary": "No evidence.", "evidence": ""}
            held = session.dispatch("new", OPEN_DRAFT, args, execute)
            self.assertIn("could not verify", held["error"])
            self.assertIsNone(luna.screen.call_args.args[0]["host_facts"]["public_draft"])
            execute.assert_not_called()
