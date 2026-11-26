import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.file_grants import FileGrants
from lantern.import_fetch import ImportRedirectNeeded
from lantern.file_imports import _transfer_slot, import_file, import_tool


class ApprovedImportsTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.spool = self.state / "objects"
        self.spool.mkdir()
        self.grants = FileGrants(self.state)
        self.data = b"time,flux\r\n0,91\r\n1,74\r\n"
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.spec = {"filename": "transit.csv", "url": "https://example.org/releases/transit",
                     "redirect_urls": ["https://data.example.org/r1"], "sha256": self.digest}
        self.destinations = {}
        self.fetch = self.enterContext(patch("lantern.file_imports.fetch_import", side_effect=self.download))
        self.delivery = self.enterContext(patch("lantern.file_imports.deliver_import", side_effect=self.publish))

    def download(self, url, redirects, temp, limit):
        Path(temp).write_bytes(self.data)
        return redirects[-1] if redirects else url

    def publish(self, socket, filename, digest):
        path = "/work/imports/" + filename
        data = (self.spool / digest).read_bytes()
        if path in self.destinations:
            if self.destinations[path] != data:
                return {"error": "Destination is occupied."}
            status = "already_present"
        else:
            self.destinations[path] = data
            status = "imported"
        return {"status": status, "path": path}

    def request(self, files=None, **kwargs):
        return self.grants.propose("sky", "transit observations", files or [self.spec], **kwargs)["id"]

    def run_import(self, number, key="call-a", filename="transit.csv"):
        return import_file(self.grants, "test.sock", {"grant_id": number, "filename": filename}, key,
                           spool=self.spool)

    def test_one_room_decision_for_collection_full_receipts_and_retries(self):
        other = {**self.spec, "filename": "control.csv"}
        number = self.request([self.spec, other])
        self.assertEqual(self.grants.status(number)["status"], "pending")
        with self.assertRaisesRegex(ValueError, "active grant"):
            self.run_import(number)
        self.fetch.assert_not_called()
        self.grants.decide(number, "allow_once")
        self.assertEqual(self.request([self.spec]), number)
        first = self.run_import(number)
        self.assertEqual(first["final_url"], "https://data.example.org/r1")
        self.assertEqual(first["url"], self.spec["url"])
        self.assertEqual((first["sha256"], first["size_bytes"]), (self.digest, len(self.data)))
        self.assertEqual(self.destinations[first["path"]], self.data)
        self.assertEqual(self.grants.status(number)["status"], "approved")
        self.run_import(number, "call-b", "control.csv")
        self.assertEqual(self.grants.status(number)["status"], "fulfilled")
        self.grants = FileGrants(self.state)
        self.assertEqual(self.run_import(number, "new-call")["status"], "previously_delivered")
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(len(self.grants.overview()["receipts"]), 2)

    def test_denial_revocation_and_distinct_mirror_require_room_decision(self):
        number = self.request()
        self.grants.decide(number, "deny")
        self.assertEqual(self.request(), number)
        self.assertEqual(self.grants.overview()["requests"], [])
        standing = self.request(operator=True)
        self.assertNotEqual(standing, number)
        self.grants.decide(standing, "allow_standing")
        self.assertEqual(self.request(), standing)
        mirror = self.request([{**self.spec, "url": "https://mirror.example.org/transit"}])
        self.assertNotEqual(mirror, standing)
        with self.assertRaisesRegex(ValueError, "active grant"):
            self.run_import(mirror)
        with self.assertRaisesRegex(ValueError, "outside the grant"):
            self.run_import(standing, filename="different.csv")
        self.grants.revoke(standing)
        with self.assertRaisesRegex(ValueError, "active grant"):
            self.run_import(standing)
        self.fetch.assert_not_called()

    def test_revocation_while_fetching_prevents_handoff(self):
        number = self.request()
        self.grants.decide(number, "allow_standing")

        def revoke(*args):
            final_url = self.download(*args)
            self.grants.revoke(number)
            return final_url

        self.fetch.side_effect = revoke
        with self.assertRaisesRegex(ValueError, "active grant"):
            self.run_import(number)
        self.delivery.assert_not_called()
        self.assertEqual(self.grants.overview()["receipts"], [])

    def test_mismatch_and_failed_fetch_do_not_spend_once_grant(self):
        number = self.request()
        self.grants.decide(number, "allow_once")
        self.fetch.side_effect = ValueError("Network timeout")
        with self.assertRaisesRegex(ValueError, "timeout"):
            self.run_import(number)
        self.fetch.side_effect = self.download
        data, self.data = self.data, b"wrong"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.run_import(number)
        self.assertEqual(list(self.spool.iterdir()), [])
        self.delivery.assert_not_called()
        self.data = data
        self.assertEqual(self.run_import(number)["status"], "imported")

    def test_recover_after_lost_delivery_reply_with_original_bytes(self):
        number = self.request()
        self.grants.decide(number, "allow_once")

        def lost(*args):
            self.publish(*args)
            return {"error": "Receipt lost"}

        self.delivery.side_effect = lost
        self.assertIn("error", self.run_import(number))
        self.assertEqual(self.grants.overview()["receipts"], [])
        self.delivery.side_effect = self.publish
        self.grants = FileGrants(self.state)
        self.assertEqual(self.run_import(number, "new-call")["status"], "already_present")
        self.fetch.assert_called_once()
        self.assertEqual(len(self.grants.overview()["receipts"]), 1)
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_interleaved_imports_keep_prepared_bytes_and_retry_identity(self):
        number = self.request()
        self.grants.decide(number, "allow_standing")
        first = self.run_import(number)
        self.delivery.side_effect = lambda *args: {"error": "Receipt lost"}
        self.run_import(number, "call-b")
        self.delivery.side_effect = self.publish
        other = self.request([{**self.spec, "filename": "other.csv"}])
        self.grants.decide(other, "allow_once")
        self.run_import(other, "call-c", "other.csv")
        self.assertTrue((self.spool / self.digest).is_file())
        replay = self.run_import(number)
        self.assertEqual(replay["delivered_at"], first["delivered_at"])
        self.assertEqual(replay["status"], "previously_delivered")
        self.assertEqual(self.run_import(number, "call-d")["status"], "already_present")
        self.assertEqual(self.run_import(number, "call-d")["status"], "previously_delivered")
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_unknown_redirect_requests_only_the_expanded_file(self):
        number = self.request([self.spec, {**self.spec, "filename": "second.csv"}])
        self.grants.decide(number, "allow_standing")
        mirror = "https://mirror.example.org/r2"
        self.fetch.side_effect = ImportRedirectNeeded(mirror)
        result = self.run_import(number)
        new = self.grants.status(result["request_id"])
        self.assertEqual(new["status"], "pending")
        self.assertEqual(new["files"], [{**self.spec, "redirect_urls": [*self.spec["redirect_urls"], mirror]}])
        self.assertEqual(self.run_import(number)["request_id"], new["id"])
        self.delivery.assert_not_called()

    def test_recovery_without_cached_copy_rejects_changed_unpinned_source(self):
        number = self.request([{k: v for k, v in self.spec.items() if k != "sha256"}])
        self.grants.decide(number, "allow_once")
        self.delivery.side_effect = lambda *args: {"error": "unavailable"}
        self.run_import(number)
        staged = self.spool / self.digest
        staged.chmod(0o600)
        staged.unlink()
        self.data += b"2,83\n"
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.run_import(number, "new-call")
        self.assertEqual(self.delivery.call_count, 1)

    def test_standing_repeat_after_deletion_and_existing_file_conflict(self):
        number = self.request()
        self.grants.decide(number, "allow_standing")
        first = self.run_import(number)
        self.assertEqual(self.run_import(number)["status"], "previously_delivered")
        self.destinations.clear()
        self.assertEqual(self.run_import(number, "new-call")["status"], "imported")
        self.destinations[first["path"]] = b"user work"
        self.assertIn("error", self.run_import(number, "third-call"))
        self.assertEqual(self.destinations[first["path"]], b"user work")
        self.assertEqual(self.grants.status(number)["status"], "approved")

    def test_concurrent_call_retries_and_validation_never_fetches(self):
        number = self.request()
        self.grants.decide(number, "allow_once")
        with _transfer_slot(self.state), self.assertRaisesRegex(ValueError, "in progress"):
            self.run_import(number)
        for spec in ({**self.spec, "filename": "../data.csv"}, {**self.spec, "sha256": self.digest[:12]},
                     {**self.spec, "url": "http://example.org/file"}, {**self.spec, "redirect_urls": ["file:///tmp"]}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.request([spec])
        self.assertIn("error", import_tool(self.grants, None, "import_file",
                                           {"grant_id": number, "filename": "transit.csv", "url": "other"}, "k"))
        self.fetch.assert_not_called()
