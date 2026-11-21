from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from lantern.web import RoomServer, SESSION_COOKIE, canonical_origin
from lantern.web_auth import LoginThrottled, SESSION_SECONDS


ORIGIN = "https://lantern.example.test"
HOST = "lantern.example.test"
PASSWORD = "A temporary passphrase for HTTP fixture tests"


@contextmanager
def listening(state, origin=ORIGIN):
    with RoomServer(state, 0, public_origin=origin) as server:
        server.publish_key()
        thread = Thread(target=server.serve_forever, args=(0.01,))
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def request(server, method, path, *, body=None, headers=None, origin=ORIGIN):
    outbound = {"Host": HOST if server.public_origin else f"127.0.0.1:{server.server_port}"}
    if body is not None:
        outbound["Content-Type"] = "application/json"
        body = json.dumps(body)
    if method == "POST" and origin is not None:
        outbound["Origin"] = origin
    outbound.update(headers or {})
    with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as connection:
        connection.request(method, path, body=body, headers=outbound)
        response = connection.getresponse()
        result = response.read()
        if response.headers.get_content_type() == "application/json":
            result = json.loads(result)
        return response.status, response.headers, result


class WebSignInTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))

    def sign_in(self, server):
        status, headers, result = request(server, "POST", "/api/sign-in", body={"password": PASSWORD})
        self.assertEqual((200, {"ok": True}), (status, result))
        value, *attributes = headers["Set-Cookie"].split("; ")
        self.assertTrue(value.startswith(f"{SESSION_COOKIE}="))
        self.assertEqual({"Path=/", f"Max-Age={SESSION_SECONDS}", "Secure", "HttpOnly", "SameSite=Strict"},
                         set(attributes))
        return {"Cookie": value}

    def test_login_shell_static_assets_and_closed_enrollment(self):
        with listening(self.state) as server:
            self.assertIsNone(server.key)
            self.assertFalse((self.state / "web-key").exists())
            server.store.submit("Synthetic private message")
            status, headers, page = request(server, "GET", "/")
            self.assertEqual(200, status)
            self.assertIn(b'type="password"', page)
            self.assertNotIn(b"Synthetic private message", page)
            self.assertEqual("no-store", headers["Cache-Control"])
            for asset in ("/room.css", "/login.js"):
                self.assertEqual(200, request(server, "GET", asset)[0])
            self.assertEqual(401, request(server, "POST", "/api/sign-in", body={"password": PASSWORD})[0])
            self.assertEqual(404, request(server, "POST", "/api/set-password", body={"password": PASSWORD})[0])
            for path in ("/api/room", "/api/room?after=0", "/api/review-questions", "/api/messages/1/image"):
                self.assertEqual(401, request(server, "GET", path)[0], path)

    def test_public_and_tunnel_auth_are_independent(self):
        with listening(self.state, None) as tunnel, listening(self.state) as public:
            public.auth.set_password(PASSWORD)
            bearer = {"Authorization": f"Bearer {tunnel.key}"}
            self.assertEqual(tunnel.key, (self.state / "web-key").read_text().strip())
            self.assertEqual(401, request(public, "GET", "/api/room", headers=bearer)[0])
            self.assertEqual(200, request(tunnel, "GET", "/api/room", headers=bearer)[0])
            cookie = self.sign_in(public)
            self.assertEqual(401, request(tunnel, "GET", "/api/room", headers=cookie)[0])
            event = {"client_id": str(uuid4()), "text": "Hello from the browser"}
            self.assertEqual(200, request(public, "POST", "/api/messages", body=event, headers=cookie)[0])
            self.assertEqual(event["text"], request(tunnel, "GET", "/api/room", headers=bearer)[2]["events"][0]["text"])

    def test_exact_origin_is_required_for_all_mutations(self):
        with listening(self.state) as server:
            server.auth.set_password(PASSWORD)
            cookie = self.sign_in(server)
            for path, body in (("/api/sign-in", {"password": PASSWORD}), ("/api/sign-out", {}),
                               ("/api/messages", {"text": "fixture", "client_id": str(uuid4())}),
                               ("/api/review-questions/1/answer", {"answer": "Fixture answer"})):
                for origin in (None, "null", "https://different.example.test"):
                    with self.subTest(path=path, origin=origin):
                        self.assertEqual(403, request(server, "POST", path, origin=origin, body=body, headers=cookie)[0])
            self.assertEqual(403, request(server, "GET", "/api/room", headers={**cookie, "Host": "different.example.test",
                                                                               "X-Forwarded-Host": HOST})[0])
            self.assertEqual(200, request(server, "GET", "/api/room", headers=cookie)[0])
            self.assertEqual([], server.store.events())

    def test_sessions_survive_restart_and_approve_a_review_once(self):
        with listening(self.state) as first:
            first.auth.set_password(PASSWORD)
            cookie = self.sign_in(first)
            first.store.submit("Synthetic reviewer fixture.")
            batch = first.store.claim()
            first.store.bind_thread(batch.id, "review-thread")
            first.store.complete(batch.id, "Waiting.")
            identifier = first.reviews.open("review-thread", "fixture", "workspace_write", "example",
                                            "Save fixture", "Sample excerpt", "May I save it?", source="prior_denial")
        with listening(self.state) as second:
            self.assertNotIn(b'type="password"', request(second, "GET", "/", headers=cookie)[2])
            questions = request(second, "GET", "/api/review-questions", headers=cookie)[2]["questions"]
            self.assertEqual([identifier], [case["id"] for case in questions])
            answer = {"answer": "Yes, save the fixture."}
            url = f"/api/review-questions/{identifier}/answer"
            self.assertEqual(401, request(second, "POST", url, body=answer)[0])
            for _ in range(2):
                status, _, receipt = request(second, "POST", url, body=answer, headers=cookie)
                self.assertEqual((200, {"id": identifier}), (status, receipt))
            self.assertEqual(answer["answer"], second.reviews.case("review-thread", "fixture")["answer"])
            self.assertEqual(1, second.store.status()["queued"])
            other_cookie = self.sign_in(second)
            self.assertNotEqual(cookie, other_cookie)
            status, headers, _ = request(second, "POST", "/api/sign-out", body={}, headers=cookie)
            self.assertEqual(200, status)
            self.assertIn("Max-Age=0", headers["Set-Cookie"])
            self.assertEqual(401, request(second, "GET", "/api/room", headers=cookie)[0])
            self.assertEqual(200, request(second, "GET", "/api/room", headers=other_cookie)[0])
            self.assertEqual(1, second.auth.revoke_all())
            self.assertEqual(401, request(second, "GET", "/api/room", headers=other_cookie)[0])

    def test_auth_route_rejects_bad_payloads_and_reports_a_retry_time(self):
        with listening(self.state) as server:
            credentials = {"password": PASSWORD}
            self.assertEqual(415, request(server, "POST", "/api/sign-in", body=credentials,
                                          headers={"Content-Type": "text/plain"})[0])
            self.assertEqual(400, request(server, "POST", "/api/sign-in", body={"other": PASSWORD})[0])
            self.assertEqual(413, request(server, "POST", "/api/sign-in", body={"password": "x" * 4100})[0])
            with patch.object(server.auth, "sign_in", side_effect=LoginThrottled(17)):
                status, headers, result = request(server, "POST", "/api/sign-in", body=credentials)
            self.assertEqual((429, "17"), (status, headers["Retry-After"]))
            self.assertNotIn(PASSWORD, str(result))
            self.assertNotIn("Set-Cookie", headers)

    def test_only_bare_https_origins_configure_public_mode(self):
        self.assertEqual(ORIGIN, canonical_origin("https://LANTERN.EXAMPLE.TEST:443/"))
        self.assertEqual(f"{ORIGIN}:9443", canonical_origin(f"{ORIGIN}:9443"))
        for origin in ("", "http://lantern.example.test", f"{ORIGIN}/room", f"{ORIGIN}?port=443",
                       f"{ORIGIN}#fragment", "https://user@lantern.example.test", f"{ORIGIN}:65536"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                canonical_origin(origin)
