import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, Mock, patch

from deploy.coding import receive_clone as receiver
from lantern import clone_fetch, coding_broker as broker, import_client
from lantern.clone_client import deliver_clone


REPO = "research/observation"
COMMIT = "1" * 40
DIGEST = "2" * 64


class CloneBrokerTests(TestCase):
    def test_clone_size_limits_agree_across_fetch_and_checkout(self):
        pack_limit = 32 * 1024 * 1024
        checkout_limit = 64 * 1024 * 1024
        self.assertEqual(clone_fetch.MAX_PACK_BYTES, pack_limit)
        self.assertEqual(clone_fetch.MAX_CHECKOUT_BYTES, checkout_limit)
        self.assertGreater(clone_fetch.MAX_WIRE_BYTES, pack_limit)
        self.assertEqual(broker.PACK_LIMIT, pack_limit)
        self.assertEqual(receiver.PACK_LIMIT, pack_limit)
        self.assertEqual(receiver.TREE_LIMIT, checkout_limit)
        args = broker.create_args("sha256:" + DIGEST, broker.clone_request(REPO, COMMIT, DIGEST))
        self.assertIn(f"--ulimit=fsize={checkout_limit}:{checkout_limit}", args)

    def test_fixed_protocol_container_and_paths(self):
        frame = {"clone_repo": REPO, "commit": COMMIT, "sha256": DIGEST}
        request = broker.read_command(Mock(recv=BytesIO(json.dumps(frame).encode() + b"\n").read))
        args = broker.create_args("sha256:" + DIGEST, request)
        self.assertIn("--user=65532:65532", args)
        self.assertIn("--network=none", args)
        self.assertEqual(args.count("--volume"), 1)
        self.assertEqual(args[-4:], ["/opt/lantern/receive_clone.py", REPO, COMMIT, DIGEST])
        for repo, sha, digest in [("repo", COMMIT, DIGEST), ("../bad", COMMIT, DIGEST),
                                 ("abc/.git", COMMIT, DIGEST), ("a/b.git", COMMIT, DIGEST),
                                 (REPO, "HEAD", DIGEST), (REPO, COMMIT, DIGEST[:12])]:
            with self.subTest(repo=repo, sha=sha, digest=digest), self.assertRaises(broker.BadRequest):
                broker.clone_request(repo, sha, digest)

    def test_receiver_result_distinguishes_definite_and_unknown_failures(self):
        def result(body):
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.recv.side_effect = BytesIO(json.dumps(body).encode() + b"\n").read
            with patch.object(import_client.socket, "socket", return_value=connection), \
                    patch.object(import_client.socket, "AF_UNIX", 1, create=True):
                return deliver_clone("test.sock", REPO, COMMIT, DIGEST)
        for body in ({"error": "bad tree", "definitive": True}, {"error": "broker busy"},
                     {"status": "cloned", "path": f"/work/repos/{REPO}/{COMMIT}"}):
            self.assertEqual(result(body), body)
        self.assertNotIn("definitive", result({"error": "x", "definitive": "yes"}))
        self.assertIn("error", result({"status": "cloned", "path": "/work/lantern"}))


@skipUnless(sys.platform == "linux", "Production offline Git receiver uses Linux filesystem operations")
class CloneReceiverTests(TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory()))
        (self.tmp / "files").mkdir()
        (self.tmp / "clone-stage").mkdir()
        self.repo = self.tmp / "upstream"
        self.repo.mkdir()
        self.git("init", "--template=", "--initial-branch=main", ".")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@workspace.invalid")
        (self.repo / "before").write_text("previous\n")
        self.git("add", "before")
        self.git("commit", "-m", "root")
        (self.repo / "before").unlink()
        (self.repo / "orbit").mkdir()
        self.data = b"19\r\n2\n"
        (self.repo / "orbit" / "points").write_bytes(self.data)
        (self.repo / "link").symlink_to("../private")
        self.git("add", "-A")
        self.git("commit", "-m", "observations")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        self.pack = self.make_pack()

    def git(self, *args, data=None):
        return subprocess.run(["git", *args], cwd=self.repo, env=receiver.ENV,
                              input=data, check=True, capture_output=True).stdout

    def make_pack(self, *options):
        objects = self.git("ls-tree", "-tr", "--format=%(objectname)", self.commit)
        root = self.git("rev-parse", "HEAD^{tree}")
        names = self.commit.encode() + b"\n" + root + objects
        return self.git("pack-objects", "--stdout", *options, data=names)

    def receive(self, pack=None, repo=REPO, commit=None):
        pack = self.pack if pack is None else pack
        return receiver.receive(BytesIO(pack), self.tmp, repo, commit or self.commit,
                                hashlib.sha256(pack).hexdigest())

    def test_shallow_checkout_link_text_edit_preservation_and_safe_config(self):
        self.assertEqual(self.receive(), receiver.CLONED)
        root = self.tmp / "files" / "repos" / REPO / self.commit
        self.assertEqual((root / "orbit/points").read_bytes(), self.data)
        self.assertFalse((root / "link").is_symlink())
        self.assertEqual((root / "link").read_text(), "../private")
        self.assertEqual(receiver.git(root, "rev-list", "--count", "HEAD").strip(), b"1")
        self.assertEqual(receiver.git(root, "status", "--porcelain"), b"")
        self.assertEqual(receiver.git(root, "rev-parse", "HEAD").decode().strip(), self.commit)
        (root / "orbit/points").write_text("local edit")
        (root / ".git/config").write_text("[corrupted on purpose")
        self.assertEqual(self.receive(), receiver.ALREADY_PRESENT)
        self.assertEqual((root / "orbit/points").read_text(), "local edit")
        self.assertEqual(len(list((self.tmp / "clone-stage").glob("*.json"))), 1)
        self.assertFalse((self.tmp / "clone-stage" / "checkout").exists())

    def test_repacked_retry_uses_independent_receipt_and_validates_new_wire(self):
        self.assertEqual(self.receive(), receiver.CLONED)
        different = self.make_pack("--compression=0")
        self.assertNotEqual(self.pack, different)
        root = self.tmp / "files" / "repos" / REPO / self.commit
        (root / "orbit/points").write_text("preserve me")
        self.assertEqual(self.receive(pack=different), receiver.ALREADY_PRESENT)
        self.assertEqual((root / "orbit/points").read_text(), "preserve me")
        history = self.git("pack-objects", "--stdout", "--revs", data=b"HEAD\n")
        self.assertEqual(self.receive(pack=history), receiver.BAD_PACK)
        # An editable marker and a retained pack cannot attest on their own.
        next((self.tmp / "clone-stage").glob("*.json")).unlink()
        self.assertEqual(self.receive(pack=different), receiver.OCCUPIED)
        self.assertEqual((root / "orbit/points").read_text(), "preserve me")

    def test_declines_wrong_commit_extraneous_objects_and_large_repeated_files(self):
        self.assertEqual(self.receive(commit=COMMIT), receiver.BAD_PACK)
        history = self.git("pack-objects", "--stdout", "--revs", data=b"HEAD\n")
        self.assertEqual(self.receive(pack=history), receiver.BAD_PACK)
        self.assertEqual(self.receive(pack=b"not a pack"), receiver.BAD_PACK)
        (self.repo / "orbit/other").write_bytes(self.data)
        self.git("add", "-A")
        self.git("commit", "-m", "repeated object")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        self.pack = self.make_pack()
        with patch.object(receiver, "TREE_LIMIT", len(self.data) + len("../private")):
            self.assertEqual(self.receive(), receiver.BAD_PACK)
        self.assertEqual(list((self.tmp / "clone-stage").iterdir()), [])

    def test_conflict_symlink_and_empty_destination_are_preserved(self):
        repos = self.tmp / "files" / "repos"
        repos.symlink_to(self.tmp / "clone-stage", target_is_directory=True)
        self.assertEqual(self.receive(), receiver.OCCUPIED)
        repos.unlink()
        occupied = repos / REPO / self.commit
        occupied.mkdir(parents=True)
        self.assertEqual(self.receive(), receiver.OCCUPIED)
        self.assertEqual(list(occupied.iterdir()), [])
        occupied.rmdir()
        (self.tmp / "clone-stage" / "checkout").mkdir()
        (self.tmp / "clone-stage" / "checkout" / "partial").write_text("interrupted")
        self.assertEqual(self.receive(), receiver.CLONED)
