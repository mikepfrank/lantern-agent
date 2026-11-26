"""Deliver a verified object through the local coding broker."""

import json
import socket
import time

from .coding_broker import import_request
from .coding_tools import DEADLINE_SECONDS


def deliver_import(socket_path, filename, sha256):
    request = import_request(filename, sha256)
    return _deliver(socket_path, {"import_filename": request.filename, "sha256": request.sha256},
                    "/work/imports/" + filename, "imported")


def _deliver(socket_path, fields, path, success):
    if not hasattr(socket, "AF_UNIX"):
        return {"error": "This platform cannot reach the coding workspace."}
    frame = json.dumps(fields).encode("ascii") + b"\n"
    deadline = time.monotonic() + DEADLINE_SECONDS
    data = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(DEADLINE_SECONDS)
            peer.connect(str(socket_path))
            peer.sendall(frame)
            while b"\n" not in data and len(data) < 2048:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                peer.settimeout(remaining)
                chunk = peer.recv(2048 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
        if data.endswith(b"\n") and data.count(b"\n") == 1:
            body = json.loads(data)
            if type(body) is dict:
                if (set(body) == {"path", "status"} and body["path"] == path
                        and body["status"] in (success, "already_present")):
                    return body
                definite = (success == "cloned" and set(body) == {"error", "definitive"}
                            and body["definitive"] is True)
                if (set(body) == {"error"} or definite) and isinstance(body["error"], str) and 0 < len(body["error"]) <= 250:
                    return body
    except (OSError, ValueError, RecursionError):
        pass
    return {"error": "No valid import receipt arrived from the coding workspace. It is safe to retry."}
