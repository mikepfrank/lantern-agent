from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from lantern.persona import ThreadPersonas


class ThreadPersonaTests(unittest.TestCase):
    def test_new_thread_uses_bundled_v1(self):
        path = Path(__file__).resolve().parents[1] / "lantern" / "personas" / "lantern-v1.md"
        expected = path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            records = ThreadPersonas(Path(folder))
            self.assertEqual(records.select(None, None), expected)
            self.assertIn("LANTERN — persona / context string", expected)

    def test_resume_recovers_original_even_if_operator_edits_file(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            records = ThreadPersonas(state)
            persona = records.select(None, "Lantern’s words.\n")
            records.bind("thread-one", persona)
            reopened = ThreadPersonas(state)
            self.assertEqual(reopened.select("thread-one", None), persona)
            with self.assertRaises(ValueError):
                reopened.select("thread-one", "A later edit")
            self.assertEqual(reopened.select(None, "A later edit"), "A later edit")

    def test_private_default_preserves_attachment_and_only_changes_new_threads(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            path = state / "heritage" / "Lantern.md"
            path.parent.mkdir()
            data = "\ufeffPrivate words — here.\r\n".encode("utf-8")
            path.write_bytes(data)
            records = ThreadPersonas(state, default_path=path)
            first = records.select(None, None)
            self.assertEqual(first, "Private words — here.\n")
            self.assertEqual(path.read_bytes(), data)
            records.bind("old", first)
            path.write_text("Revised", encoding="utf-8")
            self.assertEqual(records.select(None, None), "Revised")
            self.assertEqual(records.select("old", None), first)
            path.write_text("  ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty"):
                records.select(None, None)
            path.unlink()
            self.assertEqual(records.select(None, "Explicit"), "Explicit")
            self.assertEqual(records.select("old", None), first)
            with self.assertRaises(FileNotFoundError):
                records.select(None, None)

    def test_replace_pin_only_changes_the_expected_thread_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            records = ThreadPersonas(Path(folder))
            old = "The old persona.\n"
            digest = sha256(old.encode("utf-8")).hexdigest()
            records.bind("live", old)
            records.bind("other", old)
            with self.assertRaisesRegex(ValueError, "expected revision"):
                records.replace_pin("live", "0" * 64, "Mistaken replacement")
            with self.assertRaisesRegex(ValueError, "No persona"):
                records.replace_pin("missing", digest, "Wrong room")
            self.assertTrue(records.replace_pin("live", digest, "Revised"))
            self.assertFalse(records.replace_pin("live", digest, "Revised"))
            with self.assertRaisesRegex(ValueError, "expected revision"):
                records.replace_pin("live", digest, "Conflicting update")
            reopened = ThreadPersonas(Path(folder))
            self.assertEqual(reopened.select("live", None), "Revised")
            self.assertEqual(reopened.select("other", None), old)
