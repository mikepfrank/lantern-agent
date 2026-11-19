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
