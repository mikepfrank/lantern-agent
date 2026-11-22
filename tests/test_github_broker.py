from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lantern import github_broker as broker


class GitHubBrokerTests(unittest.TestCase):
    def test_workspace_receives_bytes_not_a_host_path(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            job, path = broker.prepare_job({"op": "sync", "bundle_bytes": 4}, BytesIO(b"test"), folder)
            self.assertEqual(job, {"op": "sync"})
            self.assertEqual(path, folder / "upstream.bundle")
            self.assertEqual(path.read_bytes(), b"test")
            for request in ({"op": "sync", "path": "/etc"}, {"op": "sync", "bundle_bytes": True},
                            {"op": "sync", "bundle_bytes": broker.BUNDLE_LIMIT + 1},
                            {"op": "preview", "commit": "--version"}):
                with self.subTest(request=request), self.assertRaises(ValueError):
                    broker.prepare_job(request, BytesIO(b""), folder)
        with TemporaryDirectory() as temp, self.assertRaises(ValueError):
            broker.prepare_job({"op": "sync", "bundle_bytes": 9}, BytesIO(b"short"), Path(temp))

    def test_preview_uses_read_only_coding_volume_without_network_or_token(self):
        with TemporaryDirectory() as temp:
            request = {"op": "preview", "commit": "c" * 40}
            self.assertEqual(broker.prepare_job(request, BytesIO(), Path(temp)), (request, None))
            image = "sha256:" + "f" * 64
            args = broker.job_args(image, None)
            self.assertIn("--network=none", args)
            self.assertIn("--user=65532:65532", args)
            self.assertIn(f"{broker.docker.VOLUME}/files:/work:ro,z", args)
            self.assertNotIn("github-token", " ".join(args))
            self.assertEqual(args[-2:], ["-I", "/opt/lantern/git_workspace.py"])
            sync = broker.job_args(image, Path(temp) / "upstream.bundle")
            self.assertIn(f"{broker.docker.VOLUME}/files:/work:rw,z", sync)
            self.assertTrue(any(arg.endswith(":/input/upstream.bundle:ro,z") for arg in sync))

    def test_bounded_object_frame(self):
        body = broker.frame({"op": "sync"})
        self.assertEqual(broker.read_frame(BytesIO(body), len(body)), {"op": "sync"})
        for payload in (body[:-1], body, b"[]\n"):
            limit = len(body) - 1 if payload == body else len(body)
            with self.assertRaises(ValueError):
                broker.read_frame(BytesIO(payload), limit)
