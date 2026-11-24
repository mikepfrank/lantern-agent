import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lantern.workspace import MAX_BYTES, MAX_TEXT, workspace_edit_evidence, workspace_tool
from lantern.workspace_diff import MAX_HUNK_BYTES


class WorkspaceDiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.root = self.temp / "files"
        self.root.mkdir()

    def preview(self, new, old=None, **args):
        proposal = {"path": "note", "text": new, **args}
        if old is not None:
            data = old.encode() if isinstance(old, str) else old
            (self.root / "note").write_bytes(data)
            proposal["expected_sha256"] = hashlib.sha256(data).hexdigest()
        return workspace_edit_evidence(self.root, proposal)

    def test_new_file_entire_proposal_is_new_without_creating_directories_or_lock_files(self):
        result = self.preview("my note", path="nested/new/note")
        self.assertEqual((result["status"], result["complete"], result["base_sha256"], result["added_from"]),
                         ("create", True, None, "arguments.text"))
        self.assertNotIn("my note", str(result))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(list(self.temp.iterdir()), [self.root])
        self.assertEqual(self.preview("", old="")["status"], "unchanged")

    def test_return_exact_unicode_spans_preserving_line_endings_and_eof(self):
        for old, new in (("Original\r\ntext\n", "Original\r\ntext\nAdded\r\n"),
                         ("🕯middle\r\n\rText!", "🕯MIDDLE\n\rtext!\n"),
                         ("repeated\n" * 2, "repeated\n"), ("", "new"), ("old", ""),
                         ("123\u2028abc\n", "123\nabc")):
            with self.subTest(old=old):
                result = self.preview(new, old)
                self.assertEqual(result["status"], "replace")
                self.assertTrue(result["complete"])
                patched = old
                for change in reversed(result["hunks"]):
                    start = change["old_offset"]
                    end = start + len(change["removed"])
                    self.assertEqual(patched[start:end], change["removed"])
                    self.assertEqual(new[change["new_offset"]:][:len(change["added"])], change["added"])
                    patched = patched[:start] + change["added"] + patched[end:]
                self.assertEqual(patched, new)
                self.assertEqual((self.root / "note").read_bytes(), old.encode())
                self.assertEqual(result["proposed_sha256"], hashlib.sha256(new.encode()).hexdigest())

    def test_two_changed_spans_in_a_long_note_keep_carried_text_out_of_hunks(self):
        middle = "Saved reading-log label\n" * 1200
        old = "A\n" + middle + "B\n"
        new = "C\n" + middle + "D\n"
        edit = self.preview(new, old)
        self.assertTrue(edit["complete"])
        self.assertEqual([(h["removed"], h["added"]) for h in edit["hunks"]], [("A", "C"), ("B", "D")])
        self.assertNotIn("Saved", json.dumps(edit))

    def test_large_and_fragmented_changes_mark_omissions_and_bound_bytes(self):
        rows = [f"row {i}\n" for i in range(150)]
        variants = (("a" * MAX_TEXT, "𐐷" * MAX_TEXT),
                    ("".join(rows), "".join(row if i % 2 else "changed\n" for i, row in enumerate(rows))))
        for old, new in variants:
            with self.subTest(length=len(new)):
                edit = self.preview(new, old)
                self.assertFalse(edit["complete"])
                self.assertGreater(edit["omitted_hunks"], 0)
                self.assertLessEqual(len(json.dumps(edit)), MAX_HUNK_BYTES + 512)

    def test_unverified_inputs_never_include_baseline_content(self):
        private = "MATERIAL WHICH MUST NEVER ENTER THE PACKET"
        (self.root / "note").write_text(private, encoding="utf-8")
        digest = hashlib.sha256(private.encode()).hexdigest()
        for args in ({}, {"expected_sha256": "0" * 64}, {"expected_sha256": None},
                     {"expected_sha256": digest, "text": "x" * (MAX_TEXT + 1)},
                     {"expected_sha256": digest, "path": "../note"},
                     {"expected_sha256": digest, "path": "missing/a"}, {"unexpected": True}):
            result = workspace_edit_evidence(self.root, {"path": "note", "text": "new", **args})
            self.assertEqual((result["status"], result["complete"]), ("unavailable", False))
            self.assertNotIn(private, str(result))
            self.assertNotIn("hunks", result)
        for invalid in (b"\xff" + private.encode(), private.encode() + b"X" * MAX_BYTES):
            result = self.preview("new", invalid)
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn(private, str(result))
        self.assertIn("12", self.preview("new", expected_sha256="deadbeef")["reason"])
        binary = b"\xff\xfe"
        digest = hashlib.sha256(binary).hexdigest()
        (self.root / "note").write_bytes(binary)
        evidence = self.preview("new", expected_sha256=digest[:12])
        self.assertEqual((evidence["status"], evidence["base_sha256"]), ("unavailable", digest))

    def test_preview_never_opens_hardlinked_file_even_with_correct_digest(self):
        private = self.temp / "private"
        private.write_bytes(b"PRIVATE outside the permitted workspace")
        try:
            os.link(private, self.root / "note")
        except PermissionError:
            if os.name == "nt":
                self.skipTest("Creating hard links is unavailable in this sandbox.")
            raise
        result = self.preview("new", expected_sha256=hashlib.sha256(private.read_bytes()).hexdigest())
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("PRIVATE", str(result))

    def test_preview_pins_parent_and_rejects_reparse_or_symbolic_links(self):
        private = self.temp / "private"
        private.write_bytes(b"PRIVATE outside the permitted workspace")
        try:
            (self.root / "note").symlink_to(private)
            (self.root / "folder").symlink_to(self.temp, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Creating Windows symlinks requires developer mode or local privilege.")
            raise
        for path in ("note", "folder/private"):
            result = self.preview("new", path=path, expected_sha256=hashlib.sha256(private.read_bytes()).hexdigest())
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(self.preview("create", path="folder/new/note")["status"], "unavailable")
        self.assertFalse((self.temp / "new").exists())

    def test_executor_rechecks_the_approved_revision(self):
        original = b"first"
        digest = hashlib.sha256(original).hexdigest()
        (self.root / "note").write_bytes(original)
        evidence = self.preview("proposed", expected_sha256=digest[:16].upper())
        self.assertEqual(evidence["base_sha256"], digest)
        (self.root / "note").write_bytes(b"newer")
        result = workspace_tool(self.root, "workspace_write", {"path": "note", "text": "proposed",
                                "expected_sha256": evidence["base_sha256"]})
        self.assertIn("error", result)
        self.assertEqual((self.root / "note").read_bytes(), b"newer")
