import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, Mock, patch

from deploy.coding import receive_import as receiver
from lantern import coding_broker as broker, import_client


DATA = b"sample,value\r\n1,42\r\n2,64"
DIGEST = hashlib.sha256(DATA).hexdigest()
NAME = "measurements.csv"


class ImportDeliveryTests(TestCase):
    def test_protocol_accepts_only_one_plain_filename_and_full_digest(self):
        def read(name, digest):
            message = json.dumps({"import_filename": name, "sha256": digest}).encode() + b"\n"
            return broker.read_command(Mock(recv=BytesIO(message).read))

        request = read(NAME, DIGEST)
        args = broker.create_args("sha256:" + "ab" * 32, request)
        self.assertEqual(args[-3:], ["/opt/lantern/receive_import.py", NAME, DIGEST])
        self.assertEqual(args.count("--volume"), 1)
        self.assertIn("--user=65532:65532", args)
        self.assertIn("--network=none", args)
        for name, digest in [("../data", DIGEST), ("a/b", DIGEST), ("--other", DIGEST),
                             ("a\r\nb", DIGEST), (NAME, DIGEST[:12]), (NAME, "z" * 64)]:
            with self.subTest(name=name, digest=digest), self.assertRaises(broker.BadRequest):
                read(name, digest)

    def test_client_checks_destination_and_outcome(self):
        def deliver(reply):
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.recv.side_effect = BytesIO(json.dumps(reply).encode() + b"\n").read
            with patch.object(import_client.socket, "socket", return_value=connection), \
                    patch.object(import_client.socket, "AF_UNIX", 1, create=True):
                result = import_client.deliver_import("test-socket", NAME, DIGEST)
            self.assertEqual(json.loads(connection.sendall.call_args.args[0]),
                             {"import_filename": NAME, "sha256": DIGEST})
            return result

        for status in ("imported", "already_present"):
            body = {"path": "/work/imports/" + NAME, "status": status}
            self.assertEqual(deliver(body), body)
        self.assertIn("error", deliver({"path": "/work/lantern/file", "status": "imported"}))
        self.assertIn("error", deliver({"path": "/work/imports/" + NAME, "status": "maybe"}))

    @skipUnless(sys.platform == "linux", "The coding broker runs on Linux")
    def test_broker_stdin_preserves_exact_bytes_across_pipe_writes(self):
        data = bytes(range(256)) * 11213
        expected = hashlib.sha256(data).hexdigest()
        with TemporaryDirectory() as tmp:
            directory = Path(tmp, "imports")
            helper = ("from deploy.coding.receive_import import receive; import sys; "
                      "sys.exit(receive(sys.stdin.buffer, *sys.argv[1:]))")
            peer, conn = socket.socketpair()
            with peer, conn, subprocess.Popen(
                [sys.executable, "-c", helper, str(directory), NAME, expected],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            ) as proc:
                result = broker.stream_command(conn, proc, data, limit=1024, append_newline=False)
            self.assertEqual(result, {"output": "", "exit_code": 0, "truncated": False, "timed_out": False})
            self.assertEqual((directory / NAME).read_bytes(), data)

    @skipUnless(sys.platform == "linux", "The coding broker runs on Linux")
    def test_regular_staged_bytes_and_incorrect_digest(self):
        with TemporaryDirectory() as tmp, patch.object(broker, "IMPORT_OBJECTS", Path(tmp)):
            request = broker.import_request(NAME, DIGEST)
            path = Path(tmp, DIGEST)
            path.write_bytes(DATA)
            self.assertEqual(broker.read_import_object(request), DATA)
            path.write_bytes(b"other")
            with self.assertRaisesRegex(broker.BadRequest, "SHA-256"):
                broker.read_import_object(request)
            path.unlink()
            path.symlink_to(Path(tmp, "missing"))
            with self.assertRaises(broker.BadRequest):
                broker.read_import_object(request)
            path.unlink()
            path.mkdir()
            with self.assertRaises(broker.BadRequest):
                broker.read_import_object(request)

    @skipUnless(sys.platform == "linux", "Offline helper uses the host's Linux filesystem semantics")
    def test_verified_atomic_file_and_idempotent_delivery(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp, "imports")
            def receive(data):
                return receiver.receive(BytesIO(data), directory, NAME, DIGEST)
            self.assertEqual(receive(b"corrupt"), receiver.BAD_DATA)
            self.assertEqual(list(directory.iterdir()), [])
            with patch.object(receiver, "LIMIT", 3):
                self.assertEqual(receive(DATA), receiver.BAD_DATA)
            self.assertEqual(receive(DATA), receiver.IMPORTED)
            self.assertEqual(receive(DATA), receiver.ALREADY_PRESENT)
            path = directory / NAME
            self.assertEqual(path.read_bytes(), DATA)
            self.assertEqual(path.stat().st_mode & 0o111, 0)
            path.write_bytes(b"mine")
            self.assertEqual(receive(DATA), receiver.OCCUPIED)
            self.assertEqual(path.read_bytes(), b"mine")
            path.unlink()
            outside = Path(tmp, "outside")
            outside.write_bytes(DATA)
            path.symlink_to(outside)
            self.assertEqual(receive(DATA), receiver.OCCUPIED)
            path.unlink()
            os.link(outside, path)
            self.assertEqual(receive(DATA), receiver.OCCUPIED)
            path.unlink()
            path.mkdir()
            self.assertEqual(receive(DATA), receiver.OCCUPIED)
            path.rmdir()
            directory.rmdir()
            directory.symlink_to(Path(tmp), target_is_directory=True)
            with self.assertRaises(OSError):
                receive(DATA)
            self.assertFalse(Path(tmp, NAME).exists())
