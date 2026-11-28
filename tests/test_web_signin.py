from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from lantern import file_imports
from lantern.attachments import parse_image
from lantern.pdf_tool import view_pdf_page
from lantern.web import RoomServer, SESSION_COOKIE, canonical_origin
from lantern.web_auth import LoginThrottled, SESSION_SECONDS
from test_attachments import png, upload
from test_pdf_renderer import two_page_pdf


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
            for path in ("/api/room", "/api/room?after=0", "/api/export", "/api/review-questions", "/api/file-imports",
                         "/api/repository-clones", "/api/messages/1/image", "/api/messages/1/images/1"):
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
            self.assertEqual(event["text"], request(public, "GET", "/api/export", headers=cookie)[2]["events"][0]["text"])

    def test_exact_origin_is_required_for_all_mutations(self):
        with listening(self.state) as server:
            server.auth.set_password(PASSWORD)
            cookie = self.sign_in(server)
            for path, body in (("/api/sign-in", {"password": PASSWORD}), ("/api/sign-out", {}),
                               ("/api/messages", {"text": "fixture", "client_id": str(uuid4())}),
                               ("/api/review-questions/1/answer", {"answer": "Fixture answer"}),
                               ("/api/file-imports", {"project": "science", "collection": "test", "files": []}),
                               ("/api/file-imports/1/decide", {"decision": "allow_standing"}),
                               ("/api/file-imports/1/revoke", {}),
                               ("/api/repository-clones/1/decide", {"decision": "allow_once"}),
                               ("/api/repository-clones/1/revoke", {}),
                               ("/api/repository-clones/1/reopen", {}),
                               ("/api/concerns/1/release", {"version": 1})):
                for origin in (None, "null", "https://different.example.test"):
                    with self.subTest(path=path, origin=origin):
                        self.assertEqual(403, request(server, "POST", path, origin=origin, body=body, headers=cookie)[0])
            self.assertEqual(403, request(server, "GET", "/api/room", headers={**cookie, "Host": "different.example.test",
                                                                               "X-Forwarded-Host": HOST})[0])
            self.assertEqual(200, request(server, "GET", "/api/room", headers=cookie)[0])
            self.assertEqual([], server.store.events())

    def test_grant_named_files_together_from_a_signed_in_room(self):
        data = {"project": "Observatory", "collection": "Night one", "files": [
            {"filename": "counts.csv", "url": "https://example.org/data/counts.csv",
             "redirect_urls": ["https://example.org/mirror/counts.csv"], "sha256": "ab" * 32},
            {"filename": "sky.txt", "url": "https://example.org/data/sky.txt"}]}
        route = "/api/file-imports"
        with listening(self.state) as server:
            server.auth.set_password(PASSWORD)
            self.assertEqual(401, request(server, "POST", route, body=data)[0])
            cookie = self.sign_in(server)
            def post(path, body):
                return request(server, "POST", path, body=body, headers=cookie)[0]
            def overview():
                return request(server, "GET", route, headers=cookie)[2]
            self.assertEqual(200, post(route, data))
            snapshot = overview()
            self.assertEqual([], snapshot["grants"])
            pending, = snapshot["requests"]
            self.assertEqual(data["files"][0]["redirect_urls"], pending["files"][0]["redirect_urls"])
            decide = f"{route}/{pending['id']}/decide"
            self.assertEqual(401, request(server, "POST", decide, body={"decision": "allow_once"})[0])
            self.assertEqual(400, post(decide, {"decision": "permit_all"}))
            self.assertEqual(200, post(decide, {"decision": "allow_standing"}))
            snapshot = overview()
            self.assertEqual([], snapshot["requests"])
            approved, = snapshot["grants"]
            self.assertEqual(("standing", 2), (approved["mode"], len(approved["files"])))
            revoke = f"{route}/{approved['id']}/revoke"
            self.assertEqual(400, post(revoke, {"other": True}))
            self.assertEqual(200, post(revoke, {}))
            self.assertEqual([], overview()["grants"])
            self.assertEqual(200, post(route, data))
            reopened, = overview()["requests"]
            self.assertEqual(200, post(f"{route}/{reopened['id']}/decide", {"decision": "deny"}))
            self.assertFalse(overview()["grants"])
            notice, = request(server, "GET", "/api/room", headers=cookie)[2]["events"]
            self.assertEqual("system", notice["role"])
            for expected in (f"#{approved['id']}", "counts.csv", "sky.txt", "repeat import", "import_file"):
                self.assertIn(expected, notice["text"])
            self.assertEqual(1, server.store.status()["queued"])

    def test_agent_pdf_request_can_be_approved_imported_and_viewed(self):
        source = "https://example.org/papers/handout.pdf"
        document = two_page_pdf()
        digest = sha256(document).hexdigest()
        spec = {"filename": "handout.pdf", "url": source, "sha256": digest}
        spool = self.state / "test-staging"
        spool.mkdir()
        socket = "test-coding-broker"
        target = "/work/imports/handout.pdf"

        def fetch(url, redirects, output, limit):
            self.assertEqual((url, redirects), (source, []))
            output.write_bytes(document)
            return url

        def deliver(broker, filename, verified):
            self.assertEqual((broker, filename, verified), (socket, spec["filename"], digest))
            self.assertEqual((spool / verified).read_bytes(), document)
            return {"path": target, "status": "imported"}

        with listening(self.state) as server, patch.object(file_imports, "OBJECTS", spool), \
                patch.object(file_imports, "fetch_import", side_effect=fetch) as fetcher, \
                patch.object(file_imports, "deliver_import", side_effect=deliver) as broker:
            def call(name, args, key):
                return file_imports.import_tool(server.file_grants, socket, name, args, key)

            proposal = call("request_file_import", {"project": "Research", "collection": "Paper",
                                                    "files": [spec]}, "proposal")
            self.assertEqual(proposal["status"], "pending")
            args = {"grant_id": proposal["id"], "filename": spec["filename"]}
            self.assertIn("error", call("import_file", args, "early"))
            fetcher.assert_not_called()
            decide = f"/api/file-imports/{proposal['id']}/decide"
            decision = {"decision": "allow_once"}
            self.assertEqual(401, request(server, "POST", decide, body=decision)[0])
            server.auth.set_password(PASSWORD)
            cookie = self.sign_in(server)
            ready = Barrier(2)
            def approve():
                ready.wait(timeout=5)
                return request(server, "POST", decide, headers=cookie, body=decision)[0]
            with ThreadPoolExecutor(max_workers=2) as pool:
                clicks = [pool.submit(approve) for _ in range(2)]
                self.assertEqual([200, 200], [click.result() for click in clicks])
            notice, = request(server, "GET", "/api/room", headers=cookie)[2]["events"]
            self.assertEqual("system", notice["role"])
            self.assertIn(spec["filename"], notice["text"])
            self.assertIn("import_file", notice["text"])
            self.assertIn("one delivery", notice["text"])
            wake, = server.store.claim().messages
            self.assertEqual((notice["id"], notice["text"]), (wake.id, wake.text))
            receipt = call("import_file", args, "approved")
            self.assertEqual((receipt["status"], receipt["path"], receipt["size_bytes"], receipt["sha256"]),
                             ("imported", target, len(document), digest))
            broker.assert_called_once()
            image = parse_image(upload(png()))
            with patch("lantern.pdf_client.render_pdf_page", return_value=(image, 2)) as render:
                page = view_pdf_page({"path": receipt["path"], "page": 2}, socket_path=socket,
                                     heritage_enabled=False, store=server.store, thread_id="room", call_id="view")
            render.assert_called_once_with(socket, target, 2)
            self.assertEqual((page["path"], page["page"], page["page_count"]), (target, 2, 2))
            self.assertEqual(image.data, server.store.page_image_data(page["page_image"]["id"], "view", thread_id="room"))

    def test_respond_to_an_agent_repository_request_in_the_signed_in_room(self):
        route = "/api/repository-clones"
        remote = {"repository_id": 21, "branch": "research", "commit": "ab" * 20}
        with listening(self.state) as server:
            with patch("lantern.clone_grants.resolve_branch", return_value=remote):
                proposal = server.clone_grants.propose("example/science", "Inspect the data.")
            server.auth.set_password(PASSWORD)
            cookie = self.sign_in(server)
            def post(path, body):
                return request(server, "POST", path, body=body, headers=cookie)[0]
            def overview():
                return request(server, "GET", route, headers=cookie)[2]
            self.assertEqual(404, post(route, {"repository": "example/other"}))
            self.assertEqual([proposal], overview()["requests"])
            base = f"{route}/{proposal['id']}"
            self.assertEqual(401, request(server, "POST", base + "/decide", body={"decision": "allow_once"})[0])
            self.assertEqual(400, post(base + "/decide", {"decision": "allow_once", "commit": "ef" * 20}))
            self.assertEqual(400, post(base + "/decide", {"decision": "allow_everything"}))
            self.assertEqual(200, post(base + "/decide", {"decision": "allow_standing"}))
            grant, = overview()["grants"]
            self.assertEqual(("example/science", "research", remote["commit"], "standing"),
                             (grant["repository"], grant["branch"], grant["commit"], grant["mode"]))
            self.assertEqual(200, post(base + "/revoke", {}))
            self.assertEqual([], overview()["grants"])
            self.assertEqual("revoked", overview()["closed"][0]["status"])
            self.assertEqual(400, post(base + "/reopen", {"commit": "ff" * 20}))
            self.assertEqual(200, post(base + "/reopen", {}))
            reopened, = overview()["requests"]
            self.assertNotEqual(proposal["id"], reopened["id"])
            self.assertEqual(remote["commit"], reopened["commit"])
            base = f"{route}/{reopened['id']}"
            self.assertEqual(200, post(base + "/decide", {"decision": "deny"}))
            self.assertEqual(200, post(base + "/reopen", {}))
            again, = overview()["requests"]
            self.assertEqual(200, post(f"{route}/{again['id']}/decide", {"decision": "allow_once"}))
            self.assertEqual("once", overview()["grants"][0]["mode"])
            first, last = request(server, "GET", "/api/room", headers=cookie)[2]["events"]
            for event, grant_id, scope in ((first, proposal["id"], "future updates"),
                                            (last, again["id"], "this commit only")):
                self.assertEqual("system", event["role"])
                for expected in (f"#{grant_id}", "example/science", "research", remote["commit"], scope,
                                 "clone_repository"):
                    self.assertIn(expected, event["text"])
            self.assertEqual(2, server.store.status()["queued"])

    def test_failed_notice_rolls_back_the_grant_and_retry_survives_restart(self):
        with listening(self.state) as server:
            server.auth.set_password(PASSWORD)
            cookie = self.sign_in(server)
            file = server.file_grants.propose("Weather", "Station", [
                {"filename": "wind.csv", "url": "https://example.org/wind.csv"}])
            remote = {"repository_id": 11, "branch": "weather", "commit": "ac" * 20}
            with patch("lantern.clone_grants.resolve_branch", return_value=remote):
                repo = server.clone_grants.propose("example/weather", "Read station data.")
            self.assertEqual(file["id"], repo["id"])  # The two grant ledgers allocate IDs independently.
            paths = [f"/api/file-imports/{file['id']}/decide", f"/api/repository-clones/{repo['id']}/decide"]
            allow = {"decision": "allow_once"}
            with server.store._connect(write=True) as db:
                db.execute("""CREATE TRIGGER refuse_notice BEFORE INSERT ON inbox BEGIN
                              SELECT RAISE(ABORT, 'synthetic queue failure'); END""")
            for path in paths:
                self.assertEqual(503, request(server, "POST", path, body=allow, headers=cookie)[0])
            self.assertEqual([], server.store.events())
            self.assertEqual("pending", server.file_grants.status(file["id"])["status"])
            self.assertEqual("pending", server.clone_grants.status(repo["id"])["status"])
            with server.store._connect(write=True) as db:
                db.execute("DROP TRIGGER refuse_notice")
            for path in paths:
                self.assertEqual(200, request(server, "POST", path, body=allow, headers=cookie)[0])

        with listening(self.state) as restarted:
            for path in paths:
                self.assertEqual(200, request(restarted, "POST", path, body=allow, headers=cookie)[0])
            file_event, repo_event = request(restarted, "GET", "/api/room", headers=cookie)[2]["events"]
            self.assertIn("wind.csv", file_event["text"])
            self.assertIn("example/weather", repo_event["text"])
            self.assertEqual(2, restarted.store.status()["queued"])

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
