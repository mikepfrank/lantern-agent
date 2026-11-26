import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from deploy.coding import render_pdf
from lantern import coding_broker as broker, pdf_client
from test_attachments import png, upload


def two_page_pdf():
    # Graphic-only pages colored red and blue. No font or extraction dependency.
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Count 2/Kids[3 0 R 4 0 R]>>"]
    for stream_id in [5, 6]:
        objects.append(f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 30 20]/Resources<<>>/Contents {stream_id} 0 R>>".encode())
    for rgb in [b"1 0 0", b"0 0 1"]:
        drawing = rgb + b" rg 0 0 30 20 re f\n"
        objects.append(b"<</Length %d>>\nstream\n" % len(drawing) + drawing + b"endstream")
    doc = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(doc))
        doc += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(doc)
    doc += b"xref\n0 7\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % n for n in offsets)
    doc += b"trailer\n<</Size 7/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(doc)


class PdfRendererTests(unittest.TestCase):
    def test_pdf_requests_are_confined_to_one_read_only_mount(self):
        image = "sha256:" + "ab" * 32
        for path, target in [(broker.PDF_HERITAGE, "/document.pdf"), ("/work/β chart.pdf", "/work/β chart.pdf")]:
            raw = json.dumps({"pdf_path": path, "page": 2}).encode() + b"\n"
            request = broker.read_command(Mock(recv=io.BytesIO(raw).read))
            args = broker.create_args(image, request)
            self.assertEqual(args[-2:], [target, "2"])
            self.assertEqual(args.count("--volume"), 1)
            mount = args[args.index("--volume") + 1]
            self.assertTrue(mount.endswith(":ro,z"))
            self.assertEqual(broker.HERITAGE_COPY in mount, path == broker.PDF_HERITAGE)
            for value in ["--network=none", "--read-only", "--cap-drop=ALL", "--memory=256m", "--user=65532:65532"]:
                self.assertIn(value, args)
        self.assertNotIn(broker.HERITAGE_COPY, " ".join(broker.create_args(image)))
        for path in ["/work/../tmp/a.pdf", "/work//f.pdf", "/etc/shadow", "/work/a\x00.pdf",
                     "/work/file.txt", "/work/back\\slash.pdf", "/work/./a.pdf"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                broker.pdf_request(path, 1)
        for page in [True, 0, -1, "1", 1000001]:
            with self.assertRaises(ValueError):
                broker.pdf_request("/work/a.pdf", page)

    def test_client_checks_page_count_png_and_broker_diagnostics(self):
        def call(body, page=1):
            data = json.dumps(body).encode() + b"\n"
            stream = MagicMock()
            stream.__enter__.return_value = stream
            stream.recv.side_effect = io.BytesIO(data).read
            with patch("lantern.pdf_client.socket.socket", return_value=stream), \
                 patch.object(pdf_client.socket, "AF_UNIX", 1, create=True):
                result = pdf_client.render_pdf_page("fake-socket", "/work/drawing.pdf", page)
                request = json.loads(stream.sendall.call_args.args[0])
                self.assertEqual(request, {"pdf_path": "/work/drawing.pdf", "page": page})
                return result
        body = {"png": upload(png())["data"], "page_count": 2}
        raster, total = call(body, 2)
        self.assertEqual((raster.width, raster.height, total), (1, 1, 2))
        with self.assertRaisesRegex(ValueError, "out of range"):
            call({"error": "Page out of range"})
        for invalid in [{**body, "page_count": 1}, {**body, "png": "bad"}, {**body, "extra": 1}]:
            with self.assertRaisesRegex(ValueError, "Invalid"):
                call(invalid, 2)

    def test_rasterizer_shrinks_only_a_dense_page_and_preserves_page_number(self):
        calls = []
        def renderer(args, **options):
            if args[0].endswith("pdfinfo"):
                options["stdout"].write(b"Pages:           3\n")
            else:
                calls.append(args)
                size = render_pdf.MAX_IMAGE + 5000 if len(calls) == 1 else 1000
                Path(args[-1] + ".png").write_bytes(bytes(size))
        with patch.object(render_pdf.subprocess, "run", side_effect=renderer):
            answer = render_pdf.render(10, 3)
            self.assertEqual(answer["page_count"], 3)
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertEqual((call[call.index("-f") + 1], call[call.index("-l") + 1]), ("3", "3"))
            self.assertLess(int(calls[1][calls[1].index("-scale-to") + 1]), 1600)
            with self.assertRaisesRegex(ValueError, "3 pages"):
                render_pdf.render(10, 4)
            self.assertEqual(len(calls), 2)

    @unittest.skipUnless(os.name == "posix", "Requires POSIX directory descriptors")
    def test_open_document_accepts_regular_file_but_refuses_symlinks_and_fifo(self):
        native_open = os.open
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder) / "work"
            work.mkdir()
            (work / "ok.pdf").write_bytes(b"fixture")
            (work / "redirect.pdf").symlink_to(work / "ok.pdf")
            (work / "alias").symlink_to(work, target_is_directory=True)
            os.mkfifo(work / "stream.pdf")
            def open_in_fixture(path, *args, **kwargs):
                return native_open(folder if path == "/" else path, *args, **kwargs)
            with patch.object(os, "open", side_effect=open_in_fixture):
                with render_pdf.open_document("/work/ok.pdf") as fd:
                    self.assertEqual(os.read(fd, 7), b"fixture")
                for path in ["/work/redirect.pdf", "/work/alias/ok.pdf", "/work/stream.pdf"]:
                    with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                        with render_pdf.open_document(path):
                            self.fail("An indirect or special file was opened.")

    @unittest.skipUnless(os.name == "posix" and Path("/usr/bin/pdftoppm").exists()
                         and Path("/proc/self/fd").is_dir(), "Requires Linux with Poppler")
    def test_poppler_returns_distinct_real_rasters_for_two_graphic_pages(self):
        from lantern.attachments import parse_image
        with tempfile.TemporaryFile() as source:
            source.write(two_page_pdf())
            source.flush()
            first, second = (render_pdf.render(source.fileno(), p) for p in [1, 2])
        one, two = (parse_image({"mime_type": "image/png", "data": item["png"]}) for item in (first, second))
        self.assertEqual((first["page_count"], second["page_count"], one.width, two.width), (2, 2, 1600, 1600))
        self.assertNotEqual(one.sha256, two.sha256)
