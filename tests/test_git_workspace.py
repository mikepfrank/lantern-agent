import base64
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from lantern.git_workspace import process, WorkspaceError


def git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


class GitWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        self.configure(self.source)
        (self.source / "base").write_text("first\n")
        (self.source / ".gitignore").write_text("ignored\nobstruction\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-m", "base")
        self.local = self.root / "editable"
        git(self.root, "clone", "--no-hardlinks", str(self.source), str(self.local))
        self.configure(self.local)
        self.original = git(self.local, "rev-parse", "HEAD")
        self.bundle = self.root / "upstream.bundle"

    @staticmethod
    def configure(repository):
        git(repository, "config", "user.name", "Test")
        git(repository, "config", "user.email", "test@localhost")
        git(repository, "config", "core.autocrlf", "false")

    def upstream(self, filename="upstream", value="new\n"):
        path = self.source / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        git(self.source, "add", "-f", "--", filename)
        git(self.source, "commit", "-m", "upstream")
        git(self.source, "bundle", "create", str(self.bundle), "refs/heads/main")
        return git(self.source, "rev-parse", "HEAD")

    def sync(self):
        return process({"op": "sync"}, self.local, self.bundle)

    def test_fast_forward_and_repeat_and_full_patch_preview(self):
        tip = self.upstream()
        result = self.sync()
        self.assertEqual(result, {"status": "fast_forwarded", "upstream": tip, "head": tip, "branch": "main"})
        self.assertEqual(self.sync()["status"], "up_to_date")
        self.assertEqual(git(self.local, "rev-parse", "refs/remotes/upstream/main"), tip)
        (self.local / "proposal").write_text("committed text\n")
        git(self.local, "add", "proposal")
        git(self.local, "commit", "-m", "candidate")
        local_commit = git(self.local, "rev-parse", "HEAD")
        self.assertEqual(self.sync()["status"], "local_ahead")
        (self.local / "proposal").write_text("private unpublished draft\n")
        proposal = process({"op":"preview", "commit":local_commit}, self.local)
        self.assertEqual((proposal["base"], proposal["commit"]), (tip, local_commit))
        patch = base64.b64decode(proposal["patch_b64"], validate=True)
        self.assertIn(b"+committed text", patch)
        self.assertNotIn(b"private unpublished draft", patch)
        self.assertNotIn(b"candidate", patch)
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), local_commit)

    def test_dirty_then_diverged_preserves_staged_and_untracked_files(self):
        tip = self.upstream()
        (self.local / "base").write_text("staged\n")
        git(self.local, "add", "base")
        (self.local / "base").write_text("unstaged\n")
        (self.local / "scratch").write_text("mine\n")
        status = git(self.local, "status", "--porcelain")
        self.assertEqual(self.sync()["status"], "dirty")
        self.assertEqual(git(self.local, "status", "--porcelain"), status)
        self.assertEqual(git(self.local, "show", ":base"), "staged")
        self.assertEqual((self.local / "base").read_text(), "unstaged\n")
        self.assertEqual((self.local / "scratch").read_text(), "mine\n")
        self.assertEqual(git(self.local, "rev-parse", "refs/remotes/upstream/main"), tip)
        git(self.local, "add", ".")
        git(self.local, "commit", "-m", "local")
        before = git(self.local, "rev-parse", "HEAD")
        self.assertEqual(self.sync()["status"], "diverged")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), before)
        with self.assertRaisesRegex(WorkspaceError, "descend"):
            process({"op":"preview", "commit":before}, self.local)

    def test_ignored_path_obstruction_is_preserved(self):
        self.upstream("ignored")
        (self.local / "ignored").write_text("private ignored file\n")
        self.assertEqual(git(self.local, "status", "--porcelain"), "")
        self.assertEqual(self.sync()["status"], "blocked")
        self.assertEqual((self.local / "ignored").read_text(), "private ignored file\n")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.original)

    def test_symlink_directory_obstruction_is_preserved(self):
        self.upstream("obstruction/upstream")
        outside = self.root / "outside"
        outside.mkdir()
        link = self.local / "obstruction"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        self.assertEqual(self.sync()["status"], "blocked")
        self.assertTrue(link.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.original)

    def test_in_progress_and_detached_checkout_only_update_tracking_ref(self):
        tip = self.upstream()
        marker = self.local / ".git" / "rebase-merge"
        marker.mkdir()
        self.assertEqual(self.sync()["status"], "blocked")
        self.assertTrue(marker.is_dir())
        marker.rmdir()
        git(self.local, "checkout", "--detach", self.original)
        result = self.sync()
        self.assertEqual(result, {"status":"detached", "branch":None, "head":self.original, "upstream":tip})

    def test_preview_requires_full_sha_and_bounded_change(self):
        tip = self.upstream()
        self.sync()
        with self.assertRaisesRegex(WorkspaceError, "no changes"):
            process({"op":"preview", "commit":tip}, self.local)
        for commit in ("HEAD", "--help", tip[:8], tip.upper()):
            with self.subTest(commit=commit), self.assertRaisesRegex(WorkspaceError, "full"):
                process({"op":"preview", "commit":commit}, self.local)
        (self.local / "long").write_text("a" * (16 * 1024) + "\n")
        git(self.local, "add", "long")
        git(self.local, "commit", "-m", "large")
        with self.assertRaisesRegex(WorkspaceError, "12 KiB"):
            process({"op":"preview", "commit":git(self.local, "rev-parse", "HEAD")}, self.local)
