"""Opt-in Linux acceptance check using the installed Docker image and disposable fixture data."""

import argparse
from contextlib import contextmanager, ExitStack
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socketserver
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from uuid import uuid4


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from lantern import github_broker as broker, github_flow as flow, github_tools as tools
from test_github_flow import GitHubMock, git


@contextmanager
def serving(server):
    with server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            thread.join()


def socket_service(path, dispatch):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(80)
            try:
                req = broker.read_frame(self.rfile, 4096)
                result = dispatch(self.connection, self.rfile, req)
            except Exception as exc:
                result = {"error": f"Fixture service {type(exc).__name__}: {exc}"}
            self.wfile.write(broker.frame(result))
    return serving(socketserver.UnixStreamServer(str(path), Handler))


class QuietFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def acceptance(tmp, image, helper):
    source = tmp / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@localhost")
    git(source, "config", "user.name", "Fixture")
    readme = source / "README.md"
    readme.write_text("Original fixture.\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "Seed")
    seed_sha = git(source, "rev-parse", "HEAD")
    seed_bundle = tmp / "seed.bundle"
    git(source, "bundle", "create", str(seed_bundle), "refs/heads/main")
    seed_bundle.chmod(0o444)
    readme.write_text("Upstream fixture.\n", encoding="utf-8")
    git(source, "commit", "-am", "Incoming change")
    upstream = git(source, "rev-parse", "HEAD")
    www = tmp / "www"
    www.mkdir()
    git(www, "clone", "--bare", "--no-local", str(source), "source.git")
    git(www / "source.git", "update-server-info")

    volume = tmp / "volume"
    volume.mkdir()
    lock = tmp / "action.lock"
    lock.touch()
    # Copy the helper into this fixture so Docker's SELinux relabel involves
    # only disposable files. The installed image is read via its immutable ID.
    helper_copy = tmp / "git_workspace.py"
    shutil.copyfile(helper, helper_copy)
    helper_copy.chmod(0o444)
    work_socket = tmp / "work.sock"
    tool_socket = tmp / "tool.sock"
    credential = tmp / "credential"
    credential.mkdir(mode=0o700)
    (credential / "github-token").write_text("fixture-token", encoding="ascii")
    state = tmp / "flow"
    state.mkdir(mode=0o700)
    nonce = uuid4().hex
    container = "lantern-git-accept-" + nonce
    label = "org.lantern.github-acceptance=" + nonce
    stub = GitHubMock(source, upstream)
    trusted_git = flow._git

    def editor(script, *, seed=False):
        args = [broker.docker.DOCKER, "run", "--rm", "--pull=never", "--network=none",
                "--read-only", "--user=65532:65532", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--volume", f"{volume}/files:/work:rw,z", "--tmpfs=/tmp:rw,nosuid,nodev,size=16m",
                "--env=HOME=/tmp", "--env=GIT_CONFIG_NOSYSTEM=1", "--entrypoint=/bin/sh"]
        if seed:
            args.extend(["--volume", f"{seed_bundle}:/seed.bundle:ro,z"])
        return subprocess.run([*args, image, "-eu", "-c", script], check=True, capture_output=True,
                              text=True, timeout=35).stdout.strip()

    def tool(name, **args):
        result = tools.github_tool(tool_socket, name, args)
        if "error" in result:
            raise RuntimeError(f"{name}: {result['error']}")
        return result

    with ExitStack() as stack:
        subprocess.run(["mount", "-t", "tmpfs", "-o", "size=48m,nodev,nosuid", "tmpfs", str(volume)], check=True)
        stack.callback(subprocess.run, ["umount", str(volume)], check=True)
        if not volume.is_mount():
            raise RuntimeError("The disposable workspace did not mount.")
        (volume / "files").mkdir()
        os.chown(volume / "files", 65532, 65532)
        editor("git clone -q -b main /seed.bundle /work/lantern", seed=True)
        address = stack.enter_context(serving(ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietFiles, directory=www))))
        local_url = f"http://127.0.0.1:{address[1]}/source.git"

        def fixture_git(args, **kwargs):
            if args[0] == "fetch":
                assert args == ["fetch", "--no-tags", local_url, "+refs/heads/main:refs/heads/main"]
                args = ["-c", "protocol.http.allow=always", *args]
            return trusted_git(args, **kwargs)

        def api(token, method, path, payload=None):
            assert token == "fixture-token"
            return stub.request(method, path, payload)

        for module, name, value in (
            (flow, "GIT_URL", local_url), (flow, "_git", fixture_git), (flow, "_api", api),
            (broker, "WORK_SOCKET", str(work_socket)), (broker, "STATE", state),
            (broker, "CONTAINER", container), (broker, "LABEL", label), (broker, "WORK_HELPER", str(helper_copy)),
            (broker.docker, "VOLUME", volume), (broker.docker, "ACTION_LOCK", str(lock)),
        ):
            stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(credential)}))
        stack.enter_context(socket_service(work_socket, broker.run_workspace))
        stack.enter_context(socket_service(tool_socket, lambda _peer, _stream, req: broker.run_flow(req)))

        editor("cd /work/lantern && echo 'keep this edit' > scratch")
        dirty = tool("github_sync")
        assert dirty["status"] == "dirty" and dirty["head"] == seed_sha and dirty["upstream"] == upstream
        assert editor("cd /work/lantern && cat scratch && git rev-parse refs/remotes/upstream/main") == (
            "keep this edit\n" + upstream)
        editor("mv /work/lantern/scratch /work/saved-scratch")
        synced = tool("github_sync")
        assert synced["status"] == "fast_forwarded" and synced["head"] == upstream
        assert editor("cd /work/lantern && cat README.md && cat /work/saved-scratch") == (
            "Upstream fixture.\nkeep this edit")
        commit = editor("cd /work/lantern && mkdir -p docs && echo 'Fixture proposal.' > docs/proposal.md && "
                        "git add docs/proposal.md && git -c user.name=Fixture -c user.email=test@localhost "
                        "commit -qm proposal && echo 'Private edit.' >> docs/proposal.md && git rev-parse HEAD")
        preview = tool("github_preview", commit=commit)
        assert preview["paths"] == ["docs/proposal.md"] and preview["base"] == upstream
        assert "+Fixture proposal." in preview["patch"] and "Private edit." not in preview["patch"]
        prefix = preview.pop("proposal_id_prefix")
        assert prefix == preview["proposal_id"][:12]
        evidence = tools.draft_evidence(tool_socket, {"proposal_id": prefix})
        assert evidence == preview and not stub.calls
        args = {"proposal_id": evidence["proposal_id"]}
        first = tool("github_open_draft", **args)
        assert first["draft"] is True and first["state"] == "open"
        assert tool("github_open_draft", **args) == first
        assert sum(method == "POST" and path == "pulls" for method, path, _ in stub.calls) == 1
        assert "Private edit." not in json.dumps(stub.calls)
        assert editor("cd /work/lantern && tail -1 docs/proposal.md") == "Private edit."
        assert git(source, "rev-parse", "HEAD") == upstream
    print("PASS: offline Docker sync, dirty checkout preservation, committed-only preview, "
          "frozen evidence and one idempotent draft at the local API fixture.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-docker-fixture", action="store_true", required=True)
    parser.add_argument("--helper", type=Path, default=Path(broker.WORK_HELPER),
                        help="Installed or staged, operator-reviewed git_workspace.py")
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or os.geteuid() != 0 or not __debug__:
        raise SystemExit("Run on Linux as root with assertions enabled for a disposable tmpfs and Docker.")
    image = broker.docker.IMAGE_FILE.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise SystemExit("Missing or invalid installed coding image pin.")
    with TemporaryDirectory(prefix="lantern-git-accept-", dir="/var/tmp") as folder:
        acceptance(Path(folder), image, args.helper.resolve(strict=True))
