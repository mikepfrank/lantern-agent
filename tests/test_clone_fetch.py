from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern import clone_fetch as clone


class Response(BytesIO):
    def getheader(self, name, default=None):
        return "application/x-git-upload-pack-result" if name == "Content-Type" else default


def packet(data):
    return f"{len(data) + 4:04x}".encode() + data


def fake_pack():
    header = b"PACK" + struct.pack(">II", 2, 2) + b"transport fixture"
    return header + hashlib.sha1(header).digest()


class PublicGitTransfer(unittest.TestCase):
    def test_normalizes_canonical_urls_and_not_other_protocols_or_destinations(self):
        for value in ("User/R-e_p.o", "https://github.com/User/R-e_p.o.git", "https://github.com/User/R-e_p.o/"):
            self.assertEqual(clone.normalize_repository(value), "user/r-e_p.o")
        for bad in ("git@github.com:owner/repo", "http://github.com/user/repo", "owner/..",
                    "https://github.com@other.org/a/b", "https://other.org/a/b", "../repo",
                    "owner/repo?key=secret", "owner/repo/tree/main", "owner/%2e%2e", "owner/.git"):
            with self.subTest(bad=bad), self.assertRaises(clone.CloneFetchError):
                clone.normalize_repository(bad)
        self.assertEqual(clone.validate_branch("release/v1.2"), "release/v1.2")
        for bad in ("HEAD", "a..b", "a/.secret", "--help", "main.lock", "v1/", "f@{1}", "x\nfoo"):
            with self.subTest(bad=bad), self.assertRaises(clone.CloneFetchError):
                clone.validate_branch(bad)

    def test_resolves_default_branch_and_checks_exact_identity(self):
        info = {"full_name": "one/two", "id": 123, "private": False, "default_branch": "research/v1"}
        sha = "5" * 40
        good = {"ref": "refs/heads/research/v1", "object": {"type": "commit", "sha": sha}}
        with patch.object(clone, "_api", side_effect=[info, good]) as api:
            self.assertEqual(clone._resolve("one/two", None),
                             {"repository_id": 123, "commit": sha, "branch": "research/v1"})
            self.assertEqual([c.args[0] for c in api.call_args_list],
                             ["/repos/one/two", "/repositories/123/git/ref/heads/research/v1"])
        for bad in ({**info, "private": True}, {**info, "full_name": "new/name"}, {**info, "id": True}):
            with patch.object(clone, "_api", return_value=bad), self.assertRaises(clone.CloneFetchError):
                clone._resolve("one/two", None)
        for bad in ({**good, "ref": "refs/heads/other"}, {**good, "object": {"type": "tag", "sha": sha}}):
            with patch.object(clone, "_api", side_effect=[info, bad]), self.assertRaises(clone.CloneFetchError):
                clone._resolve("one/two", None)

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_real_local_upload_pack_produces_self_contained_depth_one_git_checkout(self):
        with TemporaryDirectory() as folder:
            src, dest = Path(folder) / "source", Path(folder) / "destination"
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@unit.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@unit.invalid")

            def git(*args, cwd=src, data=None):
                return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args],
                                      cwd=cwd, env=env, input=data, capture_output=True, check=True).stdout

            src.mkdir()
            git("init", "-q", "--template=")
            (src / "data.txt").write_bytes(b"first\n")
            git("add", "data.txt")
            git("commit", "-qm", "base")
            previous = git("rev-parse", "HEAD").strip()
            (src / "data.txt").write_bytes(b"second\n")
            git("add", "data.txt")
            git("commit", "-qm", "change")
            commit = git("rev-parse", "HEAD").strip().decode("ascii")

            @contextmanager
            def mock_github(host, method, path, *, headers, body):
                self.assertEqual((host, method, path), ("github.com", "POST", "/science/fixture.git/git-upload-pack"))
                self.assertNotIn("Authorization", headers)
                env["GIT_PROTOCOL"] = "version=2"
                yield Response(git("upload-pack", "--stateless-rpc", ".", data=body))

            output = BytesIO()
            with patch.object(clone, "_repository", return_value={"id": 42}), patch.object(clone, "_response", mock_github):
                result = clone._fetch("science/fixture", commit, 42, output)
            data = output.getvalue()
            self.assertEqual(result, {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
            dest.mkdir()
            git("init", "-q", "--template=", cwd=dest)
            (dest / ".git" / "shallow").write_bytes(commit.encode() + b"\n")
            git("index-pack", "--stdin", cwd=dest, data=data)
            git("checkout", "--detach", commit, cwd=dest)
            self.assertEqual(git("rev-list", "HEAD", cwd=dest).splitlines(), [commit.encode()])
            self.assertEqual((dest / "data.txt").read_bytes(), b"second\n")
            with self.assertRaises(subprocess.CalledProcessError):
                git("cat-file", "-e", previous.decode(), cwd=dest)

    def test_pack_framing_size_count_and_hash_checks(self):
        sha = "7" * 40
        pack = fake_pack()
        section = packet(b"packfile\n")
        for wire in (b"0000", section + packet(b"\x03remote error") + b"0000",
                     section + packet(b"\x01" + pack[:-1]) + b"0000", section + b"ffff",
                     section + packet(b"\x01" + pack) + b"0000later",
                     packet(b"shallow-info\n") + packet(b"shallow " + b"a"*40) + b"0001" + section):
            with self.subTest(wire=wire[:16]), self.assertRaises(clone.CloneFetchError):
                clone._pack_response(Response(wire), BytesIO(), sha)
        wire = section + packet(b"\x01" + pack) + b"0000"
        with patch.object(clone, "MAX_PACK_BYTES", len(pack)-1), self.assertRaisesRegex(clone.CloneFetchError, "24 MiB"):
            clone._pack_response(Response(wire), BytesIO(), sha)
        with patch.object(clone, "MAX_WIRE_BYTES", 10), self.assertRaisesRegex(clone.CloneFetchError, "framing"):
            clone._pack_response(Response(wire), BytesIO(), sha)
        with patch.object(clone, "MAX_ENTRIES", -1), self.assertRaisesRegex(clone.CloneFetchError, "object count"):
            clone._pack_response(Response(wire), BytesIO(), sha)

    def test_reassigned_repository_blocks_even_when_same_commit_exists(self):
        sha = "7" * 40
        with patch.object(clone, "_repository", return_value={"id": 90}), patch.object(clone, "_response") as post:
            with self.assertRaisesRegex(clone.CloneFetchError, "different repository"):
                clone._fetch("science/fixture", sha, 12, BytesIO())
            post.assert_not_called()

        @contextmanager
        def fake_response(*args, **kwargs):
            yield Response(packet(b"packfile\n") + packet(b"\x01" + fake_pack()) + b"0000")

        with patch.object(clone, "_repository", side_effect=[{"id": 12}, {"id": 90}]), \
             patch.object(clone, "_response", fake_response), self.assertRaisesRegex(clone.CloneFetchError, "changed"):
            clone._fetch("science/fixture", sha, 12, BytesIO())

    def test_parent_passes_only_explicit_fields_and_removes_partial_transfer(self):
        def child(argv, *, input, stdout, stderr, env, timeout, check):
            self.assertEqual(set(env) - {"SystemRoot", "WINDIR"}, set())
            self.assertEqual(json.loads(input), {"op": "fetch", "repo": "science/fixture",
                             "commit": "a"*40, "repository_id": 25})
            self.assertEqual(timeout, clone.DEADLINE_SECONDS)
            stdout.write(b"partial")
            stdout.flush()
            return subprocess.CompletedProcess(argv, 0, stderr=b'{"error":"fixture interrupted"}')
        with TemporaryDirectory() as folder, patch.dict(os.environ, GITHUB_TOKEN="private"), \
             patch.object(subprocess, "run", child):
            path = Path(folder, "scratch.pack")
            with self.assertRaisesRegex(clone.CloneFetchError, "fixture interrupted"):
                clone.fetch_pack("Science/Fixture", "a"*40, path, repository_id=25)
            self.assertEqual(path.read_bytes(), b"")
