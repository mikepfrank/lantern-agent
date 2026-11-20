import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.attachments import parse_image
from lantern.chat_store import ChatStore
from lantern.pdf_tool import view_pdf_page
from test_attachments import png, upload


class PageToolTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.store = ChatStore(self.state)
        self.image = parse_image(upload(png()))
        self.arguments = {"path": "heritage/lmstudio-chat.pdf", "page": 2}

    def view(self, args, *, call_id="call1", heritage=True):
        return view_pdf_page(args, socket_path="test-socket", store=self.store,
                             thread_id="room", call_id=call_id, heritage_enabled=heritage)

    def test_snapshot_survives_restart_only_at_its_reviewed_call(self):
        with patch("lantern.pdf_client.render_pdf_page", return_value=(self.image, 12)) as render:
            result = self.view(self.arguments)
            render.assert_called_once_with("test-socket", self.arguments["path"], 2)
            second = self.view(self.arguments, call_id="call2")
        self.assertEqual((2, 12, 1, 1), tuple(result[k] for k in ("page", "page_count", "width", "height")))
        ref = result["page_image"]
        self.assertEqual(second["page_image"], ref)
        reader = ChatStore(self.state)
        for call in ["call1", "call2"]:
            self.assertEqual(self.image.data, reader.page_image_data(ref["id"], call, thread_id="room"))
        self.assertIsNone(reader.page_image_data(ref["id"], "unreviewed", thread_id="room"))
        self.assertIsNone(reader.page_image_data(ref["id"], "call1", thread_id="another-room"))
        self.assertIsNone(reader.image_data(ref["id"]))
        new_image = parse_image(upload(png(pixels=b"\0abc")))
        with self.assertRaisesRegex(ValueError, "different PDF"):
            reader.save_page_image("room", "call1", new_image)
        with closing(sqlite3.connect(reader.path)) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM pdf_rasters").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT count(*) FROM pdf_views").fetchone()[0])

    def test_render_error_or_private_archive_unavailable(self):
        with patch("lantern.pdf_client.render_pdf_page", side_effect=ValueError("This PDF has 1 page.")) as render:
            self.assertIn("error", self.view(self.arguments, heritage=False))
            self.assertIn("error", self.view({**self.arguments, "page": True}))
            self.assertIn("error", self.view({**self.arguments, "extra": 1}))
            render.assert_not_called()
            self.assertEqual(self.view(self.arguments), {"error": "This PDF has 1 page."})
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM pdf_rasters").fetchone()[0])

    def test_pdf_cache_keeps_recently_viewed_pages_and_never_reuses_a_reference(self):
        def save(call, pixels):
            image = parse_image(upload(png(pixels=b"\0" + pixels)))
            return self.store.save_page_image("room", call, image)["id"]

        with patch("lantern.chat_store.MAX_CACHED_PDF_RASTERS", 2):
            first = save("a", b"xyz")
            second = save("b", b"123")
            self.assertEqual(first, save("c", b"xyz"))
            third = save("d", b"456")
            self.assertIsNotNone(self.store.page_image_data(first, "a", thread_id="room"))
            self.assertIsNone(self.store.page_image_data(second, "b", thread_id="room"))
            self.assertGreater(third, second)
            self.assertGreater(save("e", b"123"), third)
