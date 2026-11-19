import json
from io import BytesIO
import os
import socket
import subprocess
import sys
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from lantern import coding_broker as broker


class CodingBrokerTests(TestCase):
    def receive(self, raw):
        conn = Mock()
        conn.recv.side_effect = BytesIO(raw).read
        return broker.read_command(conn)

    def test_exact_utf8_command_including_newlines(self):
        script = 'echo 🕯\npython -c "print(42)"'
        raw = json.dumps({"command": script}).encode() + b"\n"
        self.assertEqual(self.receive(raw), script.encode())

    def test_rejects_additional_fields_frames_and_oversized_commands(self):
        for raw in [b'{"command":"pwd","mount":"/"}\n', b'{"command":"x"}\n{}\n',
                    b'{"command":4}\n', b'{"command":"\\ud800"}\n',
                    b'{"command":"\\u0000"}\n', b'{"command":"  "}\n',
                    json.dumps({"command": "x" * (broker.COMMAND_LIMIT + 1)}).encode() + b'\n']:
            with self.subTest(raw=raw[:30]), self.assertRaises(broker.BadRequest):
                self.receive(raw)

    def test_docker_arguments_are_fixed_and_image_content_addressed(self):
        args = broker.create_args("sha256:" + "ac" * 32)
        for option in ["--user=65532:65532", "--read-only", "--network=none", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges=true", "--pids-limit=64", "--cpus=0.5",
                       "--memory=256m", "--memory-swap=256m", "--log-driver=none", "--rm",
                       "--pull=never", "--workdir=/", "--ulimit=core=0:0"]:
            self.assertIn(option, args)
        self.assertEqual(args.count("--volume"), 1)
        self.assertNotIn("docker.sock", " ".join(args))
        for image in ["latest", "sha256:123", "--privileged"]:
            with self.subTest(image=image), self.assertRaises(RuntimeError):
                broker.create_args(image)

    def test_create_failure_still_cleans_only_our_container(self):
        with patch.object(broker, "VOLUME") as volume, patch.object(broker, "engine") as engine, \
             patch.object(broker, "cleanup") as cleanup:
            volume.is_mount.return_value = True
            engine.side_effect = subprocess.TimeoutExpired("docker", 6)
            with self.assertRaises(subprocess.TimeoutExpired):
                broker.execute(None, b"pwd", "sha256:" + "ac" * 32)
            cleanup.assert_called_once()

    def test_cleanup_filters_by_both_label_and_exact_name(self):
        with patch.object(broker, "engine", return_value=Mock(stdout=b"")) as engine:
            broker.cleanup()
            args = engine.call_args.args[0]
            self.assertIn("label=" + broker.LABEL, args)
            self.assertIn(f"name=^/{broker.NAME}$", args)
            self.assertEqual(engine.call_count, 1)

    def test_failed_cleanup_requires_service_restart(self):
        with patch.object(broker, "VOLUME") as volume, patch.object(broker, "engine") as engine, \
             patch.object(broker, "cleanup", side_effect=OSError("unavailable")):
            volume.is_mount.return_value = True
            engine.side_effect = OSError("daemon unavailable")
            with self.assertRaises(SystemExit):
                broker.execute(None, b"pwd", "sha256:" + "ac" * 32)

    @skipUnless(os.name == "posix", "Docker host pipe and Unix socket multiplexing requires POSIX")
    def test_stream_bounds_and_disconnect(self):
        def stream(program, disconnect=False):
            peer, conn = socket.socketpair()
            with conn, peer, subprocess.Popen(
                [sys.executable, "-c", program], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
            ) as proc:
                if disconnect:
                    peer.close()
                try:
                    return broker.stream_command(conn, proc, b"hello")
                finally:
                    if proc.poll() is None:
                        proc.kill()
        self.assertEqual(stream("import sys;print(input());sys.exit(7)"),
                         {"exit_code": 7, "output": "hello\n", "timed_out": False, "truncated": False})
        overflow = stream("import sys;sys.stdout.write('x'*1000000)")
        self.assertTrue(overflow["truncated"])
        self.assertEqual(len(overflow["output"]), broker.OUTPUT_LIMIT)
        with patch.object(broker, "RUN_SECONDS", 0.1):
            self.assertTrue(stream("import time;time.sleep(10)")["timed_out"])
        with self.assertRaises(ConnectionAbortedError):
            stream("import time;time.sleep(10)", disconnect=True)
