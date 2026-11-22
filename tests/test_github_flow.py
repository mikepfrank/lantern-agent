import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern import github_flow
from lantern.git_workspace import process
from lantern.github_flow import ApiError, GitHubFlow, ProposalError, REPO


def git(cwd, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True).stdout.decode("utf-8").strip()


class GitHubMock:
    """Store trees, commits, refs and draft PRs independently of the broker's result."""

    def __init__(self, remote, base):
        self.remote = remote
        self.base = base
        self.branch_sha = None
        self.pull = None
        self.calls = []
        self.commits = {}
        self.drop_after_create_ref = False

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "GET" and path.startswith("pulls?"):
            return [self.pull] if self.pull else []
        if method == "GET" and path == "git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if method == "POST" and path == "git/trees":
            assert data["base_tree"] == git(self.remote, "rev-parse", self.base + "^{tree}")
            for entry in data["tree"]:
                dest = self.remote / entry["path"]
                if entry.get("sha", False) is None:
                    dest.unlink()
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(entry["content"].encode())
            git(self.remote, "add", "-A")
            return {"sha": git(self.remote, "write-tree")}
        if method == "POST" and path == "git/commits":
            sha = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
            self.commits[sha] = {"sha": sha, "tree": {"sha": data["tree"]}, "message": data["message"],
                                 "parents": [{"sha": x} for x in data["parents"]]}
            return self.commits[sha]
        if method == "GET" and path.startswith("git/commits/"):
            return self.commits[path.split("/")[-1]]
        if method == "POST" and path == "git/refs":
            if self.branch_sha:
                raise ApiError(422)
            self.branch_sha = data["sha"]
            if self.drop_after_create_ref:
                self.drop_after_create_ref = False
                raise ProposalError("Lost response")
            return {"object": {"sha": self.branch_sha}}
        if method == "GET" and path.startswith("git/ref/heads/lantern/proposal-"):
            if self.branch_sha is None:
                raise ApiError(404)
            return {"object": {"sha": self.branch_sha}}
        if method == "POST" and path == "pulls":
            self.pull = {"number": 3, "state": "open", "draft": data["draft"],
                         "base": {"ref": data["base"], "repo": {"full_name": REPO}},
                         "head": {"ref": data["head"], "repo": {"full_name": REPO}}}
            return self.pull
        raise AssertionError((method, path))


class GitHubFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.repo = self.root / "upstream"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        for key, value in (("user.email", "fixture@example.test"), ("user.name", "Fixture"), ("core.autocrlf", "false")):
            git(self.repo, "config", key, value)
        (self.repo / "README.md").write_bytes(b"public source\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Initial base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.local = self.root / "Lantern"
        git(self.root, "clone", "--no-hardlinks", str(self.repo), str(self.local))
        self.flow = GitHubFlow(self.root / "broker", self.workspace, token="fixture")
        original = github_flow._git

        def hermetic(args, **kw):
            # The one fixed public fetch is replaced by the fixture, preserving the rest of the live path.
            if args[0] == "fetch":
                self.assertEqual(args[2], github_flow.GIT_URL)
                return git(self.root, "--git-dir", str(kw["repo"]), "fetch", "--no-tags",
                           str(self.repo), args[3]).encode()
            return original(args, **kw)
        self.enterContext(patch.object(github_flow, "_git", hermetic))
        self.flow.sync()
        self.api = GitHubMock(self.repo, self.base)
        self.flow._request = self.api.request

    def workspace(self, req, bundle):
        return process(req, self.local, bundle)

    def change(self, path="README.md", content=b"public source\nApproved feature\n"):
        dest = self.local / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        git(self.local, "add", "-f", "--", path)
        git(self.local, "-c", "user.name=Offline", "-c", "user.email=local@example.test",
            "commit", "-m", "private local message")
        return git(self.local, "rev-parse", "HEAD")

    def test_review_and_publish_exact_tree_with_retry_after_created_branch(self):
        self.change("tests/internal.txt", b"temporary text\n")
        tip = self.change("tests/internal.txt", b"final public text\n")
        (self.local / "tests/internal.txt").write_text("uncommitted private change\n")
        packet = self.flow.preview(tip)
        self.assertEqual(packet, self.flow.evidence(packet["proposal_id"]))
        self.assertEqual(self.flow.preview(tip), packet)
        self.assertEqual(packet["paths"], ["tests/internal.txt"])
        self.assertEqual(self.api.calls, [])
        self.assertNotIn("temporary text", packet["patch"])
        self.assertIn("+final public text", packet["patch"])
        self.api.drop_after_create_ref = True
        with self.assertRaisesRegex(ProposalError, "Lost response"):
            self.flow.open_draft(packet["proposal_id"])
        result = self.flow.open_draft(packet["proposal_id"])
        self.assertEqual(result["url"], f"https://github.com/{REPO}/pull/3")
        self.assertIs(result["draft"], True)
        sent = json.dumps(self.api.calls)
        for secret in ("temporary text", "private local message", "uncommitted private change", tip):
            self.assertNotIn(secret, sent)
        self.assertEqual([d["parents"] for m, p, d in self.api.calls if p == "git/commits"], [[self.base]] * 2)
        self.assertEqual(self.api.pull["base"]["ref"], "main")
        self.api.pull["state"] = "closed"
        self.assertEqual(self.flow.open_draft(packet["proposal_id"])["state"], "closed")
        self.assertEqual(sum(m == "POST" and p == "pulls" for m, p, _ in self.api.calls), 1)

    def test_private_paths_and_sensitive_contents_never_reach_github(self):
        for path in ["heritage/transcript.txt", ".github/workflows/ci.yml", "lantern/.env", "docs/id_rsa",
                     "docs/notes/november.md", "deploy/test.sqlite"]:
            with self.subTest(path=path):
                git(self.local, "reset", "--hard", self.base)
                with self.assertRaises(ProposalError):
                    self.flow.preview(self.change(path))
        git(self.local, "reset", "--hard", self.base)
        with self.assertRaisesRegex(ProposalError, "credential"):
            self.flow.preview(self.change(content=b"secret: ghp_" + b"x" * 30 + b"\n"))
        self.assertEqual(self.api.calls, [])

    def test_existing_exact_branch_gets_missing_draft_after_main_advances(self):
        packet = self.flow.preview(self.change())
        self.api.drop_after_create_ref = True
        with self.assertRaisesRegex(ProposalError, "Lost response"):
            self.flow.open_draft(packet["proposal_id"])
        git(self.repo, "reset", "--hard", self.base)
        (self.repo / "another.md").write_text("New public main\n")
        git(self.repo, "add", "another.md")
        git(self.repo, "commit", "-m", "Advance main")
        self.api.base = git(self.repo, "rev-parse", "HEAD")
        self.api.calls.clear()
        receipt = self.flow.open_draft(packet["proposal_id"])
        self.assertTrue(receipt["draft"])
        self.assertEqual([p for m, p, _ in self.api.calls if m == "POST"], ["pulls"])

    def test_tampered_preview_missing_token_and_stale_base(self):
        packet = self.flow.preview(self.change())
        identity = packet["proposal_id"]
        self.flow.token = None
        with self.assertRaisesRegex(ProposalError, "credential"):
            self.flow.open_draft(identity)
        self.assertFalse(self.api.calls)
        self.flow.token = "fixture"
        self.api.base = "1" * 40
        with self.assertRaisesRegex(ProposalError, "moved"):
            self.flow.open_draft(identity)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))
        stored = self.flow.state / (identity + ".json")
        stored.write_text(stored.read_text().replace("Approved feature", "Altered feature"))
        with self.assertRaisesRegex(ProposalError, "fingerprint"):
            self.flow.open_draft(identity)

    def test_foreign_branch_and_upstream_actions_hold_publication(self):
        packet = self.flow.preview(self.change())
        self.api.branch_sha = "2" * 40
        with self.assertRaisesRegex(ProposalError, "different commit"):
            self.flow.open_draft(packet["proposal_id"])
        self.assertIsNone(self.api.pull)
        git(self.repo, "reset", "--hard", self.base)
        workflow = self.repo / ".github/workflows/ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: Fixture")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Add a workflow")
        git(self.local, "reset", "--hard", self.base)
        self.assertEqual(self.flow.sync()["status"], "fast_forwarded")
        self.api.base = git(self.repo, "rev-parse", "HEAD")
        packet = self.flow.preview(self.change())
        self.api.calls.clear()
        with self.assertRaisesRegex(ProposalError, "workflows"):
            self.flow.open_draft(packet["proposal_id"])
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))
