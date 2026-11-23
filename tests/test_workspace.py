from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.workspace import MAX_TEXT, workspace_tool


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "files"
        self.root.mkdir()
        self.private = self.root.parent / "private.txt"
        self.private.write_text("private", encoding="utf-8")

    def call(self, name, **args):
        return workspace_tool(self.root, "workspace_" + name, args)

    def test_new_file_unicode_pages_and_conflict_checked_replace(self):
        path = "projects/lantern/notes.md"
        first = self.call("write", path=path, text="🕯\nfirst")
        self.assertEqual((self.root / path).read_bytes(), "🕯\nfirst".encode())
        part = self.call("read", path=path, length=2)
        self.assertEqual(part["text"], "🕯\n")
        self.assertEqual(self.call("read", path=path, offset=part["next_offset"])["text"], "first")
        self.assertEqual(part["sha256"], first["sha256"])
        self.assertIn("error", self.call("write", path=path, text="overwrite"))
        second = self.call("write", path=path, text="second", expected_sha256=part["sha256"])
        self.assertNotEqual(second["sha256"], first["sha256"])
        self.assertIn("error", self.call("write", path=path, text="stale", expected_sha256=part["sha256"]))
        self.assertEqual(self.call("read", path=path)["text"], "second")

    def test_paginated_sorted_listing(self):
        for path in ("z", "a/k", "b"):
            self.call("write", path=path, text="")
        a = self.call("list", limit=2)
        self.assertEqual(a["entries"], [{"name": "a", "directory": True, "size_bytes": None},
                                        {"name": "b", "directory": False, "size_bytes": 0}])
        b = self.call("list", path=".", offset=a["next_offset"])
        self.assertEqual(b["entries"], [{"name": "z", "directory": False, "size_bytes": 0}])
        self.assertIsNone(b["next_offset"])
        self.assertEqual(self.call("list", path="a")["entries"],
                         [{"name": "k", "directory": False, "size_bytes": 0}])

    def test_listing_reports_file_sizes(self):
        self.call("write", path="notes/big.md", text="\U0001F56F" * 100)
        self.call("write", path="notes/empty.md", text="")
        sizes = {e["name"]: e["size_bytes"] for e in self.call("list", path="notes")["entries"]}
        self.assertEqual(sizes, {"big.md": 400, "empty.md": 0})
        top = self.call("list", path=".")["entries"]
        self.assertEqual(top, [{"name": "notes", "directory": True, "size_bytes": None}])

    def test_reject_traversal_device_names_and_invalid_inputs(self):
        for path in ("../private.txt", "/a", "a/../x", r"..\x", "a:stream", "aux.txt", "foo/CON", "a//b",
                     "NUL", "./x", "dir./x", ".lantern-tmp-0", "x\0"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call("write", path=path, text="")
        for args in ({"length": False}, {"offset": -1}, {"path": []}, {"offset": 0, "other": ""}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call("read", **{"path": "a", **args})
        with self.assertRaises(ValueError):
            self.call("write", path="a", text="." * (MAX_TEXT + 1))
        with self.assertRaises(ValueError):
            self.call("write", path="a", text="x", expected_sha256=None)
        self.assertIn("error", self.call("read", path="absent"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_hardlinked_private_file_is_rejected(self):
        try:
            os.link(self.private, self.root / "link")
        except PermissionError:
            if os.name == "nt":
                self.skipTest("The Windows sandbox disallows creating hard links.")
            raise
        for operation, args in (("read", {}), ("write", {"text": "changed"})):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.call(operation, path="link", **args)
        listed = {e["name"]: e["size_bytes"] for e in self.call("list", path=".")["entries"]}
        self.assertIsNone(listed["link"])
        self.assertEqual(self.private.read_text(), "private")

    def test_symlink_target_and_symlink_directory_are_rejected(self):
        try:
            (self.root / "link").symlink_to(self.private)
            (self.root / "folder").symlink_to(self.root.parent, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Creating Windows symlinks requires developer mode or local privilege.")
            raise
        for name, args in (("read", {"path": "link"}), ("write", {"path": "link", "text": "x"}),
                           ("read", {"path": "folder/private.txt"}), ("list", {"path": "folder"}),
                           ("write", {"path": "folder/new.txt", "text": "x"})):
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                self.call(name, **args)
        self.assertEqual(self.private.read_text(), "private")
        self.assertFalse((self.root.parent / "new.txt").exists())

    def test_failed_replacement_retains_original_and_removes_temporary(self):
        old = self.call("write", path="note", text="original")
        with patch("lantern.workspace.os.replace", side_effect=OSError("test")), self.assertRaises(ValueError):
            self.call("write", path="note", text="new", expected_sha256=old["sha256"])
        self.assertEqual(self.call("read", path="note")["text"], "original")
        self.assertEqual([p.name for p in self.root.iterdir()], ["note"])

    def test_concurrent_edits_can_commit_only_one_revision(self):
        initial = self.call("write", path="note", text="original")
        with ThreadPoolExecutor(max_workers=2) as pool:
            changes = [pool.submit(self.call, "write", path="note", text=value, expected_sha256=initial["sha256"])
                       for value in ("first", "second")]
            results = [call.result() for call in changes]
        self.assertEqual(sum("sha256" in result for result in results), 1)
        self.assertEqual(sum("error" in result for result in results), 1)
        self.assertIn(self.call("read", path="note")["text"], ("first", "second"))
