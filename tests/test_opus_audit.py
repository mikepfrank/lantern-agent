from contextlib import closing
from decimal import Decimal
from functools import partial
import hashlib
from http.client import HTTPResponse
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from lantern import opus
from lantern.luna import MODEL as LUNA_MODEL
from lantern.opus_report import opus_report
from lantern.review_vendors import HTTPRejection, ReviewerVendors, VendorError


def wire_reply(text):
    data = {
        "model": opus.OPUS_MODEL, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 211, "output_tokens": 37},
    }
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\r\n  ").encode("utf-8")


def http_response(body, status=200, *, declared_length=None):
    length = len(body) if declared_length is None else declared_length
    headers = (f"HTTP/1.1 {status} Fixture\r\nContent-Length: {length}\r\n"
               "X-Private-Diagnostic: response-header-marker\r\n\r\n").encode("ascii")
    sock = Mock()
    sock.makefile.return_value = BytesIO(headers + body)
    response = HTTPResponse(sock)
    response.begin()
    return response


class OpusAuditTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, OPENAI_API_KEY="fixture-openai",
                                     ANTHROPIC_API_KEY="fixture-anthropic-key"))
        self.connection = self.enterContext(patch("lantern.review_vendors.http.client.HTTPSConnection")).return_value
        self.vendors = ReviewerVendors(self.state, Decimal("5"))
        self.call = partial(self.vendors.call, thread_id="fixture-thread", batch_id=8,
                            tool="workspace_write", tool_call_id="fixture-call")
        self.case = {"rationale": "request-only-marker"}

    def rows(self):
        with closing(sqlite3.connect(self.vendors.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("""
                SELECT r.*, c.id, c.status AS billing, c.batch_id, c.thread_id, c.tool, c.purpose
                FROM opus_responses r JOIN calls c ON c.id=r.request_id ORDER BY c.rowid
            """)]

    def assert_archived(self, row, body, *, capture="complete"):
        directory = self.state / "opus-responses"
        path = directory / (row["id"] + ".body")
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(row["body_bytes"], len(body))
        self.assertEqual(row["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(row["capture_status"], capture)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_original_entity_preserved_separately_from_audit_metadata(self):
        body = wire_reply('<review decision="deny">Ask Mike first — privé.</review>')
        self.connection.getresponse.return_value = http_response(body)
        decision = opus.review(self.call, self.case)
        self.assertEqual(decision["decision"], "deny")
        self.assertEqual(decision["reason"], "Ask Mike first — privé.")
        self.assertEqual(decision["response_text"], '<review decision="deny">Ask Mike first — privé.</review>')
        row, = self.rows()
        self.assertEqual((row["http_status"], row["billing"], row["validation"]), (200, "metered", "verdict_deny"))
        self.assertEqual((row["thread_id"], row["batch_id"], row["tool"], row["purpose"]),
                         ("fixture-thread", 8, "workspace_write", "escalation"))
        self.assert_archived(row, body)
        for path in self.state.rglob("*"):
            if path.is_file():
                saved = path.read_bytes()
                for marker in (b"request-only-marker", b"fixture-anthropic-key", b"response-header-marker"):
                    self.assertNotIn(marker, saved)
                if path.suffix != ".body":
                    self.assertNotIn("privé".encode("utf-8"), saved)

    def test_unreadable_verdict_remains_held_and_original_is_inspectable(self):
        body = wire_reply('Commentary outside the root. <review decision="allow">Raw reply marker.')
        self.connection.getresponse.return_value = http_response(body)
        with self.assertRaises(ValueError) as failed:
            opus.review(self.call, self.case)
        self.assertNotIn("Raw reply marker", str(failed.exception))
        self.assertEqual(self.connection.getresponse.call_count, 1)
        row, = self.rows()
        self.assertEqual((row["validation"], row["billing"]), ("malformed_xml", "metered"))
        self.assert_archived(row, body)

    def test_capture_precedes_http_json_and_size_checks(self):
        cases = [
            (403, b"denied\xff\x00", HTTPRejection, "http_403"),
            (200, b"malformed\xff\n", opus.OpusResponseError, "invalid_json"),
            (200, b"A" * 2_000_110 + b"tail", opus.OpusResponseError, "response_too_large"),
        ]
        for status, body, expected, code in cases:
            with self.subTest(status=status, code=code):
                self.connection.getresponse.return_value = http_response(body, status)
                with self.assertRaises(expected):
                    opus.review(self.call, self.case)
                row = self.rows()[-1]
                self.assertEqual((row["http_status"], row["failure_code"]), (status, code))
                self.assert_archived(row, body)

    def test_partial_and_no_response_are_distinguished(self):
        self.connection.getresponse.return_value = http_response(b"partial", declared_length=270)
        with self.assertRaises(VendorError):
            opus.review(self.call, self.case)
        partial_row, = self.rows()
        self.assertEqual((partial_row["http_status"], partial_row["failure_code"]), (200, "incomplete_body"))
        self.assert_archived(partial_row, b"partial", capture="partial")
        self.connection.getresponse.side_effect = TimeoutError("fixture internal timeout")
        with self.assertRaises(VendorError):
            opus.review(self.call, self.case)
        no_response = self.rows()[-1]
        self.assertEqual((no_response["capture_status"], no_response["http_status"]), ("no_response", None))
        self.assertFalse((self.state / "opus-responses" / (no_response["id"] + ".body")).exists())

    def test_advice_repair_uses_two_distinct_receipts_and_luna_is_excluded(self):
        bad = wire_reply("First reply outside XML")
        good = wire_reply("<advice>Consult the operator.</advice>")
        luna = json.dumps({"model": LUNA_MODEL, "usage": {"input_tokens": 23, "output_tokens": 4}}).encode()
        self.connection.getresponse.side_effect = [http_response(raw) for raw in (bad, good, luna)]
        self.assertEqual(opus.consult(self.call, self.case)["advice"], "Consult the operator.")
        self.call("luna", {"model": LUNA_MODEL, "max_output_tokens": 20}, purpose="tool")
        first, second = self.rows()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([r["validation"] for r in (first, second)], ["malformed_xml", "advice"])
        self.assertTrue(all(r["purpose"] == "consult" for r in (first, second)))
        self.assert_archived(first, bad)
        self.assert_archived(second, good)
        self.assertEqual(len(list((self.state / "opus-responses").glob("*.body"))), 2)
        report = opus_report(self.state, batch=8)
        self.assertEqual([(r["request_id"], r["tool_call_id"]) for r in report["responses"]],
                         [(second["id"], "fixture-call"), (first["id"], "fixture-call")])
        self.assertEqual([Path(r["body_path"]).read_bytes() for r in report["responses"]], [good, bad])
        for marker in ("First reply outside XML", "Consult the operator.", "request-only-marker"):
            self.assertNotIn(marker, json.dumps(report))
        self.assertEqual(opus_report(self.state, batch=9)["responses"], [])
