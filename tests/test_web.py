from contextlib import closing
import base64
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
import uuid

from lantern.chat_store import ChatStore
from lantern.operator_review import ReviewRoom
from lantern.web import PAGE_SIZE, RoomServer
from test_attachments import chunk, png, upload


class MessageFormattingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "requires Node for the browser renderer")
    def test_render_saved_markdown(self):
        subprocess.run(["node", str(Path(__file__).with_name("message_format.cjs"))], check=True, timeout=20)


class WebRoomTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.room = self.enterContext(RoomServer(self.state, port=0))
        self.store = ChatStore(self.state)
        thread = threading.Thread(target=self.room.serve_forever, args=(0.01,))
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.room.shutdown)

    def request(self, method, path, *, auth=True, headers=None, body=None):
        request_headers = {}
        if auth:
            request_headers["Authorization"] = "Bearer " + self.room.key
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        request_headers.update(headers or {})
        with closing(HTTPConnection("127.0.0.1", self.room.server_port, timeout=4)) as client:
            client.request(method, path, body=body, headers=request_headers)
            response = client.getresponse()
            data = response.read()
            if response.headers.get_content_type() == "application/json":
                data = json.loads(data)
            return response.status, response.headers, data

    def test_static_shell_and_private_routes(self):
        self.store.submit("private content")
        for path in ("/", "/room.css", "/room.js", "/markdown-it-15.0.3.min.js", "/message-format.js"):
            code, headers, content = self.request("GET", path, auth=False)
            self.assertEqual(200, code)
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn(b"private content", content)
            self.assertNotIn(self.room.key.encode(), content)
        self.assertEqual(401, self.request("GET", "/api/room", auth=False)[0])
        self.assertEqual(401, self.request("POST", "/api/messages", auth=False)[0])
        self.assertEqual(401, self.request("GET", "/api/room?key=" + self.room.key, auth=False)[0])
        self.assertEqual(404, self.request("GET", "/../chat.sqlite")[0])
        self.assertEqual(404, self.request("GET", "/web-key")[0])

    def test_host_and_origin_validation_precede_writes(self):
        message = {"text": "hello", "client_id": str(uuid.uuid4())}
        for headers in ({"Host": "outside.example"}, {"Origin": "null"},
                        {"Origin": "https://outside.example"}, {"Origin": "http://127.0.0.1:12"}):
            self.assertEqual(403, self.request("POST", "/api/messages", headers=headers, body=message)[0])
        self.assertEqual([], self.store.events())
        self.assertEqual(200, self.request("POST", "/api/messages", headers={
            "Origin": f"http://127.0.0.1:{self.room.server_port}"}, body=message)[0])

    def open_review(self):
        self.store.submit("Review this private note.")
        batch = self.store.claim()
        self.store.bind_thread(batch.id, "review-thread")
        self.store.complete(batch.id, "Waiting for clarification.")
        return self.room.reviews.open(
            "review-thread", "workspace_write:notes/example.md", "workspace_write", "original-call",
            "Save notes/example.md", "A short excerpt.", "May Lantern save this note?", source="prior_denial")

    def test_review_questions_require_operator_auth_and_valid_origin(self):
        review_id = self.open_review()
        inbox = "/api/review-questions"
        answer_path = f"{inbox}/{review_id}/answer"
        answer = {"answer": "Yes, for this note."}
        for method, path, body in (("GET", inbox, None), ("POST", answer_path, answer)):
            with self.subTest(method=method):
                self.assertEqual(401, self.request(method, path, auth=False, body=body)[0])
                for headers in ({"Host": "outside.example"}, {"Origin": "null"},
                                {"Origin": "https://outside.example"}):
                    self.assertEqual(403, self.request(method, path, headers=headers, body=body)[0])
        self.assertEqual(404, self.request("POST", f"{inbox}/{review_id + 1}/answer", body=answer)[0])
        for bad in ({}, {"answer": " "}, {"answer": 1}, {"answer": "x" * 2001}, {"answer": "\ud800"},
                    {"answer": "approved", "source": "operator"}):
            with self.subTest(keys=list(bad)):
                self.assertEqual(400, self.request("POST", answer_path, body=bad)[0])
        self.assertEqual(413, self.request("POST", answer_path, body={"answer": "x" * 128001})[0])
        self.assertEqual([review_id], [q["id"] for q in self.room.reviews.pending()])
        self.assertEqual(0, self.store.status()["queued"])

    def test_review_answer_is_verbatim_durable_and_idempotent(self):
        review_id = self.open_review()
        inbox = "/api/review-questions"
        status, headers, page = self.request("GET", inbox)
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        question, = page["questions"]
        self.assertEqual(review_id, question["id"])
        self.assertEqual("prior_denial", question["source"])
        self.assertEqual("Save notes/example.md", question["action"])
        self.assertEqual("A short excerpt.", question["excerpt"])
        self.assertEqual("May Lantern save this note?", question["question"])
        self.assertTrue(question["asked_at"])
        answer = {"answer": "  Yes, for this note.\nKeep my wording. 🏮  "}
        path = f"{inbox}/{review_id}/answer"
        origin = {"Origin": f"http://127.0.0.1:{self.room.server_port}"}
        for _ in range(2):
            status, _, receipt = self.request("POST", path, headers=origin, body=answer)
            self.assertEqual((200, {"id": review_id}), (status, receipt))
        self.assertEqual(409, self.request("POST", path, body={"answer": "A different answer."})[0])
        self.assertEqual({"questions": []}, self.request("GET", inbox)[2])
        reopened = ChatStore(self.state)
        case = ReviewRoom(reopened).case("review-thread", "workspace_write:notes/example.md")
        self.assertEqual(answer["answer"], case["answer"])
        self.assertEqual(1, reopened.status()["queued"])
        wake, = reopened.claim().messages
        self.assertEqual((case["wake_id"], "system"), (wake.id, wake.role))

    def test_send_retry_cli_interleave_and_independent_cursors(self):
        cli_event = self.store.submit("Hello from the terminal")
        self.store.mark_read(cli_event)
        message = {"text": "Follow-up\nfrom the browser 🏮", "client_id": str(uuid.uuid4())}
        status, _, receipt = self.request("POST", "/api/messages", body=message)
        self.assertEqual(200, status)
        self.assertEqual(receipt, self.request("POST", "/api/messages", body=message)[2])
        self.assertEqual(409, self.request("POST", "/api/messages", body={**message, "text": "different"})[0])
        batch = self.store.claim()
        self.assertEqual([cli_event, receipt["id"]], [event.id for event in batch.messages])
        interim = self.store.notify(batch.id, "Still working", "test-call")
        status, _, page = self.request("GET", f"/api/room?after={cli_event}")
        self.assertEqual(200, status)
        self.assertTrue(page["status"]["working"])
        self.assertEqual([message["text"], "Still working"], [e["text"] for e in page["events"]])
        final = self.store.complete(batch.id, "Finished")
        page = self.request("GET", f"/api/room?after={interim}")[2]
        self.assertEqual([final], [e["id"] for e in page["events"]])
        self.assertFalse(page["status"]["working"])
        self.assertEqual(cli_event, self.store.read_cursor())

    def test_private_image_message_history_preview_and_receipts(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMwLNoFAAI1AV5D0BXUAAAAAElFTkSuQmCC"
        message = {"text": "", "client_id": str(uuid.uuid4()), "image": {"mime_type": "image/png", "data": png}}
        self.assertEqual(401, self.request("POST", "/api/messages", auth=False, body=message)[0])
        self.assertEqual(403, self.request("POST", "/api/messages", headers={"Origin": "null"}, body=message)[0])
        status, _, receipt = self.request("POST", "/api/messages", body=message)
        self.assertEqual(200, status)
        self.assertEqual(receipt, self.request("POST", "/api/messages", body=message)[2])
        self.assertEqual(409, self.request("POST", "/api/messages", body={**message, "text": "Changed"})[0])
        self.assertEqual(400, self.request("POST", "/api/messages", body={**message, "image": {"url": "http://x"}})[0])
        status, _, page = self.request("GET", "/api/room")
        event, = page["events"]
        self.assertEqual((200, "", 1, 1), (status, event["text"], event["image"]["width"], event["image"]["height"]))
        self.assertNotIn(png, json.dumps(page))
        path = f"/api/messages/{receipt['id']}/image"
        self.assertEqual(401, self.request("GET", path, auth=False)[0])
        self.assertEqual(403, self.request("GET", path, headers={"Host": "outside.example"})[0])
        status, headers, pixels = self.request("GET", path)
        self.assertEqual((200, "no-store", "image/png"), (status, headers["Cache-Control"], headers["Content-Type"]))
        self.assertEqual(base64.b64decode(png), pixels)
        self.assertEqual(404, self.request("GET", "/api/messages/9223372036854775808/image")[0])
        self.assertEqual(404, self.request("GET", "/api/messages/200/image")[0])

    def test_large_uploads_require_auth_before_using_bounded_capacity(self):
        body = {"text": " " * 140000, "client_id": str(uuid.uuid4())}
        slots = self.room.large_uploads
        with slots, slots:
            self.assertEqual(401, self.request("POST", "/api/messages", auth=False, body=body)[0])
            self.assertEqual(403, self.request("POST", "/api/messages", body=body, headers={"Origin": "null"})[0])
            self.assertEqual(503, self.request("POST", "/api/messages", body=body)[0])
            self.assertEqual(200, self.request("POST", "/api/messages", body={**body, "text": "Hello"})[0])
        self.assertEqual(400, self.request("POST", "/api/messages", body=body)[0])

    def test_four_images_share_one_event_and_keep_their_order_on_retry(self):
        # Together these uploads exceed the former single-image request ceiling.
        padding = chunk(b"tEXt", b"Comment\0" + b"x" * 420_000)
        pixels = [bytes((0, n, 20, 30)) for n in (10, 40, 100, 200)]
        uploads = [upload(png(pixels=color, extra=padding)) for color in pixels]
        body = {"text": "Compare these in order", "client_id": str(uuid.uuid4()), "images": uploads}
        status, _, receipt = self.request("POST", "/api/messages", body=body)
        self.assertEqual(200, status)
        self.assertEqual(receipt, self.request("POST", "/api/messages", body=body)[2])
        self.assertEqual(409, self.request("POST", "/api/messages", body={**body, "images": uploads[::-1]})[0])
        event, = self.request("GET", "/api/room")[2]["events"]
        self.assertEqual(receipt["id"], event["id"])
        self.assertEqual(body["text"], event["text"])
        self.assertEqual(4, len(event["images"]))
        self.assertEqual(event["images"][0], event["image"])
        for index, color in enumerate(pixels):
            path = f"/api/messages/{event['id']}/images/{index}"
            self.assertEqual(401, self.request("GET", path, auth=False)[0])
            status, _, data = self.request("GET", path)
            self.assertEqual((200, png(pixels=color)), (status, data))
        self.assertEqual(404, self.request("GET", f"/api/messages/{event['id']}/images/4")[0])
        self.assertEqual(1, self.store.status()["queued"])

    def test_invalid_batch_does_not_partially_save_a_message(self):
        first = upload(png())
        base = {"text": "", "client_id": str(uuid.uuid4())}
        for fields in ({"images": [first, {}]}, {"images": [first] * 5}, {"images": first},
                       {"images": []}, {"image": first, "images": [first]}):
            with self.subTest(keys=list(fields)):
                self.assertEqual(400, self.request("POST", "/api/messages", body={**base, **fields})[0])
        self.assertEqual([], self.store.events())
        self.assertEqual(200, self.request("POST", "/api/messages", body={**base, "images": [first, first]})[0])
        self.assertEqual(1, self.store.status()["queued"])

    def test_history_pages_and_invalid_inputs(self):
        for i in range(PAGE_SIZE + 3):
            self.store.submit(str(i))
        page = self.request("GET", "/api/room")[2]
        self.assertTrue(page["more"])
        first = page["events"][0]["id"]
        older = self.request("GET", f"/api/room?before={first}")[2]
        self.assertFalse(older["more"])
        self.assertEqual(["0", "1", "2"], [row["text"] for row in older["events"]])
        initial = self.request("GET", "/api/room?after=0")[2]
        self.assertTrue(initial["more"])
        self.assertEqual(PAGE_SIZE, len(initial["events"]))
        for query in ("after=-1", "key=4", "before=9999999999999999999", "after=1&after=2", "before="):
            self.assertEqual(400, self.request("GET", "/api/room?" + query)[0])
        good_id = str(uuid.uuid4())
        for bad in ({"text": "", "client_id": good_id}, {"text": "x" * 16001, "client_id": good_id},
                    {"text": "\ud800", "client_id": good_id}, {"text": "x", "client_id": "?"}, {}):
            self.assertEqual(400, self.request("POST", "/api/messages", body=bad)[0])

    def test_published_key_is_private_and_rotates(self):
        self.room.publish_key()
        path = self.state / "web-key"
        self.assertEqual(self.room.key, path.read_text().strip())
        if os.name == "posix":
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        with RoomServer(self.state, port=0) as replacement:
            replacement.publish_key()
            self.assertNotEqual(self.room.key, path.read_text().strip())


if __name__ == "__main__":
    unittest.main()
