from email.message import Message
import gzip
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lantern import import_fetch as imports


URL = "https://example.org/data?release=3&item=a%2Fb"
MIRROR = "https://mirror.example.org/data?release=3&item=a%2Fb"


def mock_response(status=200, body=b"data", **headers):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.headers = Message()
    for key, value in headers.items():
        response.headers[key] = value
    response.read.side_effect = io.BytesIO(body).read
    return response


class ImportFetchTests(unittest.TestCase):
    def test_redirect_requires_the_exact_public_url_and_copies_raw_query_and_bytes(self):
        data = b"\xef\xbb\xbfvalue\r\n42\r\n"
        first, second = MagicMock(), MagicMock()
        first.getresponse.return_value = mock_response(302, Location=MIRROR)
        second.getresponse.return_value = mock_response(body=data)
        out = io.BytesIO()
        with patch.object(imports, "_PublicConnection", side_effect=[first, second]) as connect:
            self.assertEqual(imports._fetch(URL, [MIRROR], out, len(data)), MIRROR)
        self.assertEqual(out.getvalue(), data)
        self.assertEqual([c.args[0] for c in connect.call_args_list], ["example.org", "mirror.example.org"])
        for conn in (first, second):
            self.assertEqual(conn.request.call_args.args, ("GET", "/data?release=3&item=a%2Fb"))
            self.assertEqual(set(conn.request.call_args.kwargs["headers"]),
                             {"Accept", "Accept-Encoding", "User-Agent", "Connection"})
        for location in (MIRROR + "&extra=1", "/elsewhere", "https://127.0.0.1/a", MIRROR + "#", " " + MIRROR):
            with self.subTest(location=location), patch.object(imports, "_PublicConnection") as connect:
                connect.return_value.getresponse.return_value = mock_response(301, Location=location)
                with self.assertRaises(imports.ImportFetchError):
                    imports._fetch(URL, [MIRROR], io.BytesIO(), 10)
                self.assertEqual(connect.call_count, 1)

    def test_grant_url_and_limits_reject_before_download(self):
        for url in (URL + "#fragment", URL + "#", "https://example.org/%ZZ", "https://example.org/?token=dummy",
                    "https://127.0.0.1/", "http://example.org/", "https://user@example.org/"):
            with self.subTest(url=url), self.assertRaises(imports.ImportFetchError):
                imports.checked_import_url(url)
        with patch.object(imports.subprocess, "run") as run:
            for redirects, limit in ((MIRROR, 10), ([MIRROR] * 4, 10), ([], 0), ([], True), ([], imports.MAX_BYTES + 1)):
                with self.subTest(redirects=redirects, limit=limit), self.assertRaises(imports.ImportFetchError):
                    imports.fetch_import(URL, redirects, "unused", limit)
            run.assert_not_called()

    def test_only_valid_new_mirrors_cross_the_process_boundary(self):
        def child_response(location):
            stderr = io.StringIO()
            with patch.object(imports, "_PublicConnection") as conn, \
                 patch.object(imports.sys, "stdin", io.StringIO(json.dumps([URL, [], 4]))), \
                 patch.object(imports.sys, "stderr", stderr):
                conn.return_value.getresponse.return_value = mock_response(302, Location=location)
                imports._child()
                self.assertEqual(conn.call_count, 1)
            return stderr.getvalue().encode()

        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "staged"
            allowed = child_response(MIRROR)
            self.assertEqual(json.loads(allowed), {"redirect": MIRROR})
            with patch.object(imports.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stderr=allowed)):
                with self.assertRaises(imports.ImportRedirectNeeded) as caught:
                    imports.fetch_import(URL, [], dest, 4)
            self.assertEqual(caught.exception.url, MIRROR)
            self.assertNotIn(MIRROR, str(caught.exception))
            self.assertEqual(dest.read_bytes(), b"")
            for unsafe in (URL, "https://127.0.0.1/", "https://example.org/?token=private-value",
                           "https://example.org/#secret", "https://example.org/" + "x" * 2050):
                with self.subTest(unsafe=unsafe[:70]):
                    blocked = json.loads(child_response(unsafe))
                    self.assertEqual(set(blocked), {"error"})
                    self.assertNotIn(unsafe, blocked["error"])
                    fake = subprocess.CompletedProcess([], 0, stderr=json.dumps({"redirect": unsafe}).encode())
                    with patch.object(imports.subprocess, "run", return_value=fake):
                        with self.assertRaises(imports.ImportFetchError) as caught:
                            imports.fetch_import(URL, [], dest, 4)
                    self.assertNotIsInstance(caught.exception, imports.ImportRedirectNeeded)

    def test_excessive_redirect_does_not_generate_mirror_request(self):
        paths = [f"https://example.org/{i}" for i in range(1, 5)]
        with patch.object(imports, "_PublicConnection") as conn:
            conn.return_value.getresponse.side_effect = [mock_response(302, Location=path) for path in paths]
            with self.assertRaisesRegex(imports.ImportFetchError, "excessive") as caught:
                imports._fetch(URL, paths[:3], io.BytesIO(), 4)
            self.assertNotIsInstance(caught.exception, imports.ImportRedirectNeeded)

    def test_status_framing_and_size(self):
        ambiguous = mock_response(**{"Content-Length": "4"})
        ambiguous.headers["Content-Length"] = "4"
        truncated = mock_response(body=b"bad", **{"Content-Length": "4"})
        for response in (mock_response(206), mock_response(404), ambiguous, truncated,
                         mock_response(body=b"oversize"), mock_response(**{"Content-Length": "5"}),
                         mock_response(**{"Content-Length": "-1"}), mock_response(**{"Content-Encoding": "gzip"}),
                         mock_response(**{"Content-Range": "bytes 0-3/5"}), mock_response(**{"Transfer-Encoding": "foo"}),
                         mock_response(**{"Transfer-Encoding": "chunked", "Content-Length": "4"}),
                         mock_response(**{"Content-Type": "text/html; charset=utf-8"})):
            with self.subTest(headers=response.headers, status=response.status), patch.object(imports, "_PublicConnection") as conn:
                conn.return_value.getresponse.return_value = response
                with self.assertRaises(imports.ImportFetchError):
                    imports._fetch(URL, [], io.BytesIO(), 4)

    def test_child_deadline_sanitizes_failure_and_discards_partial_data(self):
        def timeout(*args, **kwargs):
            kwargs["stdout"].write(b"partial")
            raise subprocess.TimeoutExpired("sensitive-internal-path", 1)

        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "test"
            with patch.object(imports.subprocess, "run", side_effect=timeout) as proc:
                with self.assertRaisesRegex(imports.ImportFetchError, "deadline"):
                    imports.fetch_import(URL, [], dest, 4)
            self.assertEqual(dest.read_bytes(), b"")
            self.assertEqual(proc.call_args.kwargs["timeout"], imports.DEADLINE_SECONDS)
            self.assertFalse(set(proc.call_args.kwargs["env"]) - {"SystemRoot", "WINDIR"})
            self.assertNotIn(str(dest), str(proc.call_args.args))
            self.assertEqual(json.loads(proc.call_args.kwargs["input"]), [URL, [], 4])

    def test_formats_and_active_text_rejections(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data"
            good = (("a.txt", b"\xef\xbb\xbf# observations\r\n2 3\r\n"),
                    ("a.csv", b'x,"quoted, data"\n'), ("a.tsv", b"x\ty\n"),
                    ("a.JSON", b'{"value": [1, 2]}'), ("a.tar", bytes(1024)),
                    ("archive.tar.part02", bytes(range(256))), ("archive.tar.gz.part12", b"arbitrary"),
                    ("a.tar.001", b"first chunk"), ("a.tgz", gzip.compress(bytes(1024))))
            for name, data in good:
                with self.subTest(name=name):
                    path.write_bytes(data)
                    imports.validate_data(path, name)
                    self.assertEqual(path.read_bytes(), data)
            with tarfile.open(path, mode="w") as archive:
                member = tarfile.TarInfo("some-member")
                member.size = 1
                archive.addfile(member, io.BytesIO(b"a"))
            imports.validate_data(path, "nonempty.tar")
            bad = (("script.py", b"1"), ("x.csv", b"\xff"), ("x.csv", b'data,"unclosed\n'),
                   ("x.csv", b"a\0b"), ("x.txt", b"\n" * 70000 + b"<!doctype html>"),
                   ("x.txt", b"\xef\xbb\xbf#!/bin/sh"), ("x.txt", b"\n\t"), ("x.json", b"{"),
                   ("x.json", b"[NaN]"), ("x.tar", b"data" * 512), ("x.tgz", b"<html>error"))
            for name, data in bad:
                with self.subTest(name=name, data=data[:20]), self.assertRaises(imports.ImportFetchError):
                    path.write_bytes(data)
                    imports.validate_data(path, name)

    def test_filename_must_be_short_plain_basename(self):
        for name in ("../data.tsv", "dir/file.json", ".secret.txt", "C:\\x.csv", "bad name.txt",
                     "A" * 120 + ".txt", "code.py", "archive.zip", "a.tar.gz.sh"):
            self.assertFalse(imports.valid_filename(name), name)

    def test_binary_pdf_checks_the_final_revision_without_decoding_the_body(self):
        trailer = b"\nstartxref\n12\n%%EOF\r\n"
        body = b"\n%\xe2\xe3\xcf\xd3\n" + bytes(range(256)) * 20
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "staged"
            for version in (b"1.0", b"1.7", b"2.0"):
                data = b"%PDF-" + version + body + trailer + b"incremental update" + trailer
                path.write_bytes(data)
                imports.validate_data(path, "paper.PDF")
                self.assertEqual(path.read_bytes(), data)
            for bad in (b"<html>Missing</html>", b"%PDF-1.80" + body + trailer, b"%PDF-1.4" + body,
                        data + b"incomplete update", data[:-len(trailer)] + b"\nstartxref\n999999\n%%EOF"):
                path.write_bytes(bad)
                with self.subTest(bad_tail=bad[-32:]), self.assertRaisesRegex(imports.ImportFetchError, "PDF"):
                    imports.validate_data(path, "paper.pdf")
            with path.open("wb") as large:
                large.write(b"%PDF-1.7\n")
                large.truncate(imports.MAX_BYTES + 1)
            with self.assertRaisesRegex(imports.ImportFetchError, "size limit"):
                imports.validate_data(path, "paper.pdf")

    def test_bounded_text_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data"
            for name, data in (("view.txt", b"<meta charset=utf-8>"),
                               ("oversize.csv", b"," * (imports._TABULAR_LINE + 1)),
                               ("oversize.json", b"[" + b"0," * imports._JSON_BYTES + b"1]")):
                path.write_bytes(data)
                with self.subTest(name=name), self.assertRaises(imports.ImportFetchError):
                    imports.validate_data(path, name)


if __name__ == "__main__":
    unittest.main()
