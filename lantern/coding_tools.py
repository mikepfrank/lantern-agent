"""Client for the operator-configured, isolated coding runner."""

import json
import socket
import time


DEFAULT_SOCKET = "/run/lantern-code.sock"
COMMAND_LIMIT = 16 * 1024
OUTPUT_LIMIT = 16 * 1024
RESPONSE_LIMIT = 110 * 1024
DEADLINE_SECONDS = 55

CODING_TOOL = {
    "name": "coding_run",
    "description": (
        "Run one shell command or multiline script in an offline, unprivileged coding container. "
        "The working directory /work/lantern is your persistent, editable Git clone of the harness. "
        "Use shell, Python 3 and local Git to inspect files, edit code, run tests and commit work. "
        "Files under /work persist between calls. Each call starts a fresh shell. "
        "The operator's live harness, credentials and your separate private notes are outside this workspace. "
        "Runs stop after 30 seconds or when combined stdout and stderr exceed 16 KiB. "
        "After an interrupted or truncated run, inspect the workspace before repeating changes."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {"command": {
            "type": "string", "minLength": 1, "maxLength": COMMAND_LIMIT,
            "description": "Shell script, up to 16 KiB of UTF-8. For Python use python3 or a here-document.",
        }},
        "required": ["command"],
    },
}


def _read_reply(peer, deadline):
    data = bytearray()
    while len(data) < RESPONSE_LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        peer.settimeout(remaining)
        chunk = peer.recv(min(8192, RESPONSE_LIMIT - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            if data.count(b"\n") != 1 or not data.endswith(b"\n"):
                break
            reply = json.loads(data.decode("utf-8"))
            if isinstance(reply, dict):
                if set(reply) == {"error"} and isinstance(reply["error"], str) and len(reply["error"]) <= 1024:
                    return reply
                if (set(reply) == {"exit_code", "output", "truncated", "timed_out"}
                        and (type(reply["exit_code"]) is int or reply["exit_code"] is None)
                        and type(reply["truncated"]) is bool and type(reply["timed_out"]) is bool
                        and isinstance(reply["output"], str) and len(reply["output"]) <= OUTPUT_LIMIT):
                    return reply
            break
    raise ValueError("Invalid or incomplete coding runner reply")


def run_code(socket_path, arguments):
    """Execute an admitted proposal once. A lost reply can follow a successful edit."""
    if not isinstance(arguments, dict) or set(arguments) != {"command"}:
        raise ValueError("Supply one shell command.")
    command = arguments["command"]
    if (not isinstance(command, str) or not command.strip() or "\0" in command
            or len(command.encode("utf-8")) > COMMAND_LIMIT):
        raise ValueError("Supply a nonempty UTF-8 shell script of up to 16 KiB.")
    frame = json.dumps({"command": command}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if not hasattr(socket, "AF_UNIX"):
        return {"error": "This platform has no Unix socket support. The command was not sent."}
    connected = False
    deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(DEADLINE_SECONDS)
            peer.connect(str(socket_path))
            connected = True
            peer.settimeout(max(0, deadline - time.monotonic()))
            peer.sendall(frame)
            return _read_reply(peer, deadline)
    except (OSError, ValueError, RecursionError):
        if connected:
            return {"error": "The coding runner reply was lost or unreadable. The command may have run. "
                             "Inspect the workspace before repeating changes."}
        return {"error": "The coding runner is unavailable. The command was not sent."}
