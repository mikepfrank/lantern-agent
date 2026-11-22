"""Two socket-activated jobs: an unprivileged GitHub client and an offline Docker runner."""

from contextlib import closing
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory

from . import coding_broker as docker


WORK_SOCKET = "/run/lantern-git-work.sock"
STATE = Path("/var/lib/lantern-github")
WORK_HELPER = "/opt/lantern/github/git_workspace.py"
CONTAINER = "lantern-github-workspace"
LABEL = "org.lantern.github-workspace=1"
BUNDLE_LIMIT = 32 * 1024 * 1024
REPLY_LIMIT = 128 * 1024


def frame(data):
    return json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode() + b"\n"


def read_frame(stream, limit):
    line = stream.readline(limit + 1)
    if len(line) > limit or not line.endswith(b"\n"):
        raise ValueError("Incomplete or oversized GitHub tool message.")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("Expected a GitHub tool request object.")
    return data


def workspace(request, bundle=None):
    # The only information crossing into the offline runner is a selector and,
    # for sync, the public upstream bundle. No caller-supplied host paths.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(75)
        peer.connect(WORK_SOCKET)
        if bundle is None:
            peer.sendall(frame(request))
        else:
            with open(bundle, "rb") as source:
                size = os.fstat(source.fileno()).st_size
                if not 0 < size <= BUNDLE_LIMIT:
                    raise ValueError("Upstream bundle exceeds the coding workspace transfer limit.")
                peer.sendall(frame({**request, "bundle_bytes": size}))
                while chunk := source.read(65536):
                    peer.sendall(chunk)
        with peer.makefile("rb") as stream:
            return read_frame(stream, REPLY_LIMIT)


def prepare_job(request, stream, directory):
    if (set(request) == {"op", "commit"} and request["op"] == "preview"
            and isinstance(request["commit"], str) and re.fullmatch("[0-9a-f]{40}", request["commit"])):
        return request, None
    if set(request) != {"op", "bundle_bytes"} or request["op"] != "sync":
        raise ValueError("Unknown offline Git operation.")
    size = request["bundle_bytes"]
    if type(size) is not int or not 0 < size <= BUNDLE_LIMIT:
        raise ValueError("Invalid upstream bundle size.")
    bundle = directory / "upstream.bundle"
    with bundle.open("xb") as output:
        while size:
            chunk = stream.read(min(size, 65536))
            if not chunk:
                raise ValueError("The upstream bundle transfer was interrupted.")
            output.write(chunk)
            size -= len(chunk)
    bundle.chmod(0o444)
    return {"op": "sync"}, bundle


def job_args(image, bundle):
    args = docker.container_args(image, CONTAINER, LABEL)
    access = "rw" if bundle else "ro"
    args.extend(["--volume", f"{docker.VOLUME}/files:/work:{access},z",
                 "--volume", f"{WORK_HELPER}:/opt/lantern/git_workspace.py:ro,z"])
    if bundle:
        args.extend(["--volume", f"{bundle}:/input/upstream.bundle:ro,z"])
    return [*args, "--workdir=/", "--env=HOME=/tmp", "--entrypoint=/usr/local/bin/python3",
            image, "-I", "/opt/lantern/git_workspace.py"]


def run_workspace(peer, stream, request):
    import fcntl
    if not docker.VOLUME.is_mount():
        raise ValueError("The coding volume is unavailable.")
    with TemporaryDirectory(dir=docker.VOLUME.parent) as tmp:
        job, bundle = prepare_job(request, stream, Path(tmp))
        with open(docker.ACTION_LOCK, "rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"error": "The coding workspace is busy. Retry after the active command finishes."}
            docker.cleanup(CONTAINER, LABEL)
            try:
                image = docker.IMAGE_FILE.read_text(encoding="ascii").strip()
                identifier = docker.engine(job_args(image, bundle)).stdout.decode("ascii").strip()
                if not re.fullmatch("[0-9a-f]{64}", identifier):
                    raise ValueError("The offline Git runner could not start.")
                with subprocess.Popen([docker.DOCKER, "start", "--attach", "--interactive", identifier],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, bufsize=0) as proc:
                    try:
                        result = docker.stream_command(peer, proc, frame(job).rstrip(b"\n"), REPLY_LIMIT)
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                if result["truncated"] or result["timed_out"] or result["exit_code"] != 0:
                    raise ValueError("The offline Git job exceeded its limits or did not finish. Inspect the checkout.")
                reply = json.loads(result["output"])
                if not isinstance(reply, dict):
                    raise ValueError("Invalid offline Git job response.")
                return reply
            finally:
                docker.cleanup(CONTAINER, LABEL)


def run_flow(request):
    import fcntl
    from .github_flow import GitHubFlow

    operations = {"sync": None, "preview": "commit", "evidence": "proposal_id", "open_draft": "proposal_id"}
    op = request.get("op")
    if not isinstance(op, str) or op not in operations:
        raise ValueError("Unknown GitHub operation.")
    field = operations[op]
    if set(request) != ({"op", field} if field else {"op"}):
        raise ValueError("Unexpected GitHub tool arguments.")
    if field and (not isinstance(request[field], str) or not re.fullmatch(
            r"[0-9a-f]{40}" if field == "commit" else r"[0-9a-f]{64}", request[field])):
        raise ValueError("Invalid GitHub tool identifier.")
    token = None
    if op == "open_draft":
        cred = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "github-token"
        token = cred.read_text().strip() or None
    with (STATE / "operations.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"error": "Another GitHub operation is in progress. Retry after it completes."}
        flow = GitHubFlow(STATE, workspace, token=token)
        operation = getattr(flow, op)
        return operation(request[field]) if field else operation()


def main(mode):
    if mode not in ("workspace", "flow"):
        raise ValueError("Unknown service mode.")
    with closing(socket.socket(fileno=0)) as peer:
        peer.settimeout(20)
        try:
            with peer.makefile("rb") as stream:
                request = read_frame(stream, 4096)
                reply = run_workspace(peer, stream, request) if mode == "workspace" else run_flow(request)
        except (ValueError, UnicodeError) as exc:
            reply = {"error": str(exc)[:500]}
        except Exception as exc:
            print(f"GitHub {mode} service: {type(exc).__name__}", file=sys.stderr)
            reply = {"error": "The GitHub operation could not complete. Check the service, then retry the same proposal."}
        peer.setblocking(True)
        peer.settimeout(5)
        data = frame(reply)
        peer.sendall(data if len(data) <= REPLY_LIMIT else frame({"error": "GitHub tool reply is too large."}))
