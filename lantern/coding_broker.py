"""Host-side Docker broker. Run from its operator-owned systemd unit only."""

import argparse
import json
import logging
import os
from pathlib import Path
import re
import selectors
import socket
import subprocess
import time
from typing import NamedTuple


DOCKER = "/usr/bin/docker"
IMAGE_FILE = Path("/etc/lantern/coding-image")
VOLUME = Path("/var/lib/lanterncode/work")
LABEL = "org.lantern.coding-broker=1"
NAME = "lantern-coding-command"
COMMAND_LIMIT = 16 * 1024
REQUEST_LIMIT = 100 * 1024
OUTPUT_LIMIT = 16 * 1024
RUN_SECONDS = 30
ENGINE_TIMEOUT = 6
PDF_HERITAGE = "heritage/lmstudio-chat.pdf"
HERITAGE_COPY = "/var/lib/lanterncode/heritage.pdf"
ACTION_LOCK = "/var/lib/lanterncode/action.lock"
PDF_REPLY_LIMIT = 1400000  # One base64-encoded 1 MiB PNG plus its small JSON envelope.


class PdfPage(NamedTuple):
    path: str
    page: int


class BadRequest(ValueError):
    pass


def pdf_request(path, page):
    if (not isinstance(path, str) or len(path) > 512 or type(page) is not int
            or not 1 <= page <= 1000000):
        raise BadRequest("Use a PDF path and a one-based page number from 1 to 1000000.")
    if path != PDF_HERITAGE and (
        not path.startswith("/work/") or not path.lower().endswith(".pdf")
        or any(part in ("", ".", "..") for part in path[6:].split("/"))
        or "\\" in path or not path.isprintable()
    ):
        raise BadRequest("Choose heritage/lmstudio-chat.pdf or an absolute PDF path under /work/.")
    return PdfPage(path, page)


def read_command(conn):
    deadline = time.monotonic() + 4
    raw = bytearray()
    while b"\n" not in raw:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BadRequest("Incomplete coding request.")
        conn.settimeout(remaining)
        chunk = conn.recv(8192)
        if not chunk:
            raise BadRequest("Incomplete coding request.")
        raw.extend(chunk)
        if len(raw) > REQUEST_LIMIT:
            raise BadRequest("Coding request exceeds the size limit.")
    try:
        if raw.index(b"\n") != len(raw) - 1:
            raise ValueError
        body = json.loads(raw.decode("utf-8"))
        if type(body) is dict and set(body) == {"pdf_path", "page"}:
            return pdf_request(body["pdf_path"], body["page"])
        if type(body) is not dict or set(body) != {"command"}:
            raise ValueError
        command = body["command"]
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError
        encoded = command.encode("utf-8")
        if len(encoded) > COMMAND_LIMIT:
            raise ValueError
        return encoded
    except BadRequest:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BadRequest("Expected one UTF-8 shell command of at most 16 KiB.") from exc


def container_args(image, name, label):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise RuntimeError("Invalid operator image pin")
    return [
        DOCKER, "create", "--name", name, "--label", label, "--pull=never",
        "--rm", "--interactive", "--network=none", "--ipc=private",
        "--read-only", "--user=65532:65532", "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true", "--pids-limit=64",
        "--cpus=0.5", "--memory=256m", "--memory-swap=256m",
        "--ulimit=nofile=256:256", "--ulimit=core=0:0", "--shm-size=8m",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777",
        "--log-driver=none",
    ]


def create_args(image, pdf=None):
    args = container_args(image, NAME, LABEL)
    if pdf is not None:
        if pdf.path == PDF_HERITAGE:
            mount, path = f"{HERITAGE_COPY}:/document.pdf:ro,z", "/document.pdf"
        else:
            mount, path = f"{VOLUME}/files:/work:ro,z", pdf.path
        return [*args, "--volume", mount, "--workdir=/", "--ulimit=fsize=12582912:12582912",
                "--env=HOME=/tmp", "--entrypoint=/usr/local/bin/python3", image,
                "-I", "/opt/lantern/render_pdf.py", path, str(pdf.page)]
    return [*args, "--volume", f"{VOLUME}/files:/work:rw,z",
        "--workdir=/", "--env=HOME=/work/.home",
        "--env=GIT_CONFIG_NOSYSTEM=1", "--entrypoint=/bin/sh", image, "-c",
        "cd /work/lantern 2>/dev/null || cd /work 2>/dev/null || :; exec /bin/sh -s",
    ]


def engine(args):
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=ENGINE_TIMEOUT, check=True)


def cleanup(name=NAME, label=LABEL):
    # Match both the exclusive name and the operator label. Never remove an
    # unrelated container that happens to occupy our reserved name.
    found = engine([DOCKER, "ps", "-aq", "--no-trunc", "--filter", f"label={label}",
                    "--filter", f"name=^/{name}$"]).stdout.decode("ascii").split()
    for container in found:
        if not re.fullmatch(r"[0-9a-f]{64}", container):
            raise RuntimeError("Unexpected container identifier")
        try:
            engine([DOCKER, "rm", "-f", container])
        except subprocess.CalledProcessError:
            # --rm may have completed between the list and remove operations.
            if engine([DOCKER, "ps", "-aq", "--filter", f"id={container}"]).stdout.strip():
                raise


def stream_command(conn, proc, command, limit=OUTPUT_LIMIT):
    """Multiplex stdin, output and disconnects without buffering unbounded data."""
    deadline = time.monotonic() + RUN_SECONDS
    output = bytearray()
    pending = memoryview(command + b"\n")
    timed_out = truncated = False
    code = None
    conn.setblocking(False)
    with selectors.DefaultSelector() as poller:
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        poller.register(proc.stdin, selectors.EVENT_WRITE, "input")
        poller.register(proc.stdout, selectors.EVENT_READ, "output")
        poller.register(conn, selectors.EVENT_READ, "client")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in poller.select(remaining):
                if key.data == "client":
                    # The protocol permits one request and keeps both
                    # directions open until its reply.
                    raise ConnectionAbortedError("Coding client disconnected or sent more data")
                if key.data == "input":
                    try:
                        written = os.write(key.fd, pending)
                        pending = pending[written:]
                    except BrokenPipeError:
                        pending = b""
                    if not pending:
                        poller.unregister(key.fd)
                        proc.stdin.close()
                else:
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        # CLI can close stdout a few instructions before exit.
                        try:
                            code = proc.wait(timeout=max(0, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            timed_out = True
                        return result(output, code, timed_out, truncated)
                    space = limit - len(output)
                    output.extend(chunk[:space])
                    if len(chunk) > space:
                        truncated = True
                        break
            if truncated:
                break
    return result(output, code, timed_out, truncated)


def result(output, code, timed_out, truncated):
    return {"exit_code": code, "output": output.decode("utf-8", errors="replace"),
            "timed_out": timed_out, "truncated": truncated}


def execute(conn, command, image):
    if not VOLUME.is_mount():
        raise RuntimeError("Coding storage is not mounted")
    pdf = command if isinstance(command, PdfPage) else None
    if pdf and pdf.path == PDF_HERITAGE and not Path(HERITAGE_COPY).is_file():
        raise RuntimeError("Private PDF has not been provisioned")
    args = create_args(image, pdf)
    try:
        # No caller controls a Docker flag, image, host command or host path.
        created = engine(args).stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", created):
            raise RuntimeError("Docker did not return a container ID")
        with subprocess.Popen([DOCKER, "start", "--attach", "--interactive", created],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, bufsize=0) as proc:
            try:
                if pdf is None:
                    return stream_command(conn, proc, command)
                result = stream_command(conn, proc, b"", limit=PDF_REPLY_LIMIT)
                if result["timed_out"]:
                    return {"error": "Rendering this PDF page exceeded the 30-second limit."}
                if result["truncated"] or result["exit_code"] != 0:
                    return {"error": "The PDF page could not be rendered within the sandbox limits."}
                return json.loads(result["output"])
            finally:
                if proc.poll() is None:
                    proc.kill()
    finally:
        # If Docker cannot confirm cleanup, fail the broker closed. systemd
        # will retry cleanup before accepting another request, even if the
        # original client never reconnects.
        try:
            cleanup()
        except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
            raise SystemExit("Container cleanup pending; restarting broker") from exc


def serve_connection(conn, image):
    command = None
    try:
        command = read_command(conn)
        reply = execute(conn, command, image)
    except BadRequest as exc:
        reply = {"error": str(exc)}
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, RecursionError) as exc:
        # Log only the error class, since stdout/stderr may contain user code.
        logging.warning("Coding request did not finish (%s)", type(exc).__name__)
        if isinstance(command, PdfPage):
            reply = {"error": "The read-only PDF renderer is unavailable or interrupted."}
        else:
            reply = {"error": "Coding sandbox unavailable or interrupted. The command may have run. "
                              "Inspect the workspace before repeating changes."}
    conn.settimeout(2)
    try:
        conn.sendall(json.dumps(reply, ensure_ascii=True).encode("ascii") + b"\n")
    except OSError:
        pass


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    cleanup()
    if args.cleanup:
        return
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("Expected one socket from systemd")
    image = IMAGE_FILE.read_text(encoding="ascii").strip()
    create_args(image)
    with open(ACTION_LOCK, "rb") as lock, socket.socket(fileno=3) as listener:
        while True:
            conn, _ = listener.accept()
            with conn:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    conn.settimeout(2)
                    try:
                        conn.sendall(b'{"error":"Coding sandbox is updating. The request did not run."}\n')
                    except OSError:
                        pass
                    continue
                try:
                    serve_connection(conn, image)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
