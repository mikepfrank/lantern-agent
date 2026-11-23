from email.message import Message
import io
import json
import socket
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from lantern import page_fetch as pages


def response(status=200, body=b"page", **headers):
    reply = MagicMock()
    reply.__enter__.return_value = reply
    reply.status = status
    reply.headers = Message()
    for name, value in headers.items():
        reply.headers[name] = value
    reply.read.side_effect = io.BytesIO(body).read
    return reply


class PageFetchTests(unittest.TestCase):
    def test_public_url_normalization_and_bounds(self):
        self.assertEqual(pages._checked_url("https://Example.org:443/à?q=café#section"),
                         "https://example.org/%C3%A0?q=caf%C3%A9")
        self.assertEqual(pages._checked_url("https://[2606:4700:4700::1111]/"),
                         "https://[2606:4700:4700::1111]/")
        for url in (None, "http://example.org/", "https://example.org:8080/",
                    "https://user@example.org/", "https://server/", "https://host.local/",
                    "https://127.0.0.1/", "https://[::1]/", "https://224.0.0.1/",
                    "https://example.org/path with space", "https://example.org/?api-key=redacted",
                    "https://example.org/" + "x" * pages.MAX_URL):
            with self.subTest(url=str(url)[:64]), self.assertRaises(pages.PageFetchError):
                pages._checked_url(url)

    def test_connect_uses_verified_sockaddr_and_tls_name(self):
        record = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))
        connection = pages._PublicConnection("example.org", port=443, timeout=5)
        with patch.object(pages.socket, "getaddrinfo", return_value=[record]) as lookup, \
             patch.object(pages.socket, "socket") as create, patch.object(connection._context, "wrap_socket") as wrap:
            connection.connect()
            lookup.assert_called_once_with("example.org.", 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            create.return_value.connect.assert_called_once_with(record[-1])
            wrap.assert_called_once_with(create.return_value, server_hostname="example.org")
            self.assertIs(connection.sock, wrap.return_value)
            connection.close()

    def test_mixed_address_result_stops_before_connecting(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                     for address in ("1.1.1.1", "127.0.0.1")]
        connection = pages._PublicConnection("example.org", port=443)
        with patch.object(pages.socket, "getaddrinfo", return_value=addresses), \
             patch.object(pages.socket, "socket") as create, self.assertRaises(pages.PageFetchError):
            connection.connect()
        create.assert_not_called()

    def test_redirect_uses_fresh_connection_and_sends_only_read_headers(self):
        first = MagicMock()
        first.getresponse.return_value = response(302, Location="https://www.example.org/next")
        second = MagicMock()
        second.getresponse.return_value = response(body=b"caf\xe9", **{"Content-Type": "text/plain; charset=windows-1252"})
        with patch.object(pages, "_PublicConnection", side_effect=[first, second]) as connections:
            page = pages._fetch("https://example.org/")
        self.assertEqual(page, pages.FetchedPage("https://www.example.org/next", "text/plain", "café"))
        self.assertEqual([call.args[0] for call in connections.call_args_list], ["example.org", "www.example.org"])
        for conn, path in ((first, "/"), (second, "/next")):
            args, kwargs = conn.request.call_args
            self.assertEqual(args, ("GET", path))
            self.assertEqual(set(kwargs["headers"]), {"Accept", "Accept-Encoding", "User-Agent", "Connection"})
            conn.close.assert_called_once()

    def test_invalid_redirect_and_redirect_limit(self):
        for location in ("http://example.org/next", "https://example.org/next?token=redacted", "/again"):
            with self.subTest(location=location), patch.object(pages, "_PublicConnection") as connect:
                connect.return_value.getresponse.return_value = response(302, Location=location)
                with self.assertRaises(pages.PageFetchError):
                    pages._fetch("https://example.org/")
                self.assertEqual(connect.call_count, pages.MAX_REDIRECTS + 1 if location == "/again" else 1)

    def test_source_bytes_mime_encoding_and_incomplete_transfer(self):
        html = {"Content-Type": "text/html"}
        cases = [
            response(**{"Content-Type": "application/octet-stream"}),
            response(**html, **{"Content-Encoding": "gzip"}),
            response(**html, **{"Content-Length": str(pages.MAX_BODY + 1)}),
            response(body=b"x" * (pages.MAX_BODY + 1), **html),
            response(body=b"x", **html, **{"Content-Length": "2"}),
        ]
        for index, reply in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(pages.PageFetchError):
                pages._response_text(reply)
        self.assertEqual(pages._response_text(response(body=b"readable", **html)), ("text/html", "readable"))

    def test_outer_deadline_and_child_errors_are_sanitized(self):
        with patch.object(pages.subprocess, "run", side_effect=subprocess.TimeoutExpired("private", 1)) as child:
            with self.assertRaisesRegex(pages.PageFetchError, "deadline"):
                pages.fetch_page("https://example.org/")
        self.assertEqual(child.call_args.kwargs["timeout"], pages.DEADLINE_SECONDS)
        self.assertNotIn("OPENROUTER_API_KEY", child.call_args.kwargs["env"])
        ok = json.dumps({"url": "https://example.org/", "content_type": "text/plain", "text": "ok"}).encode()
        with patch.object(pages.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ok)):
            self.assertEqual(pages.fetch_page("https://example.org/").text, "ok")
