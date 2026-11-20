"""Ask the local, read-only PDF renderer for one validated page image."""

import json
import socket
import time

from .attachments import parse_image
from .coding_broker import PDF_REPLY_LIMIT, pdf_request
from .coding_tools import DEADLINE_SECONDS


def render_pdf_page(socket_path, path, page):
    """Return an ImageAttachment and the document's page count."""
    request = pdf_request(path, page)
    if not hasattr(socket, "AF_UNIX"):
        raise ValueError("This platform cannot reach the private PDF renderer.")
    frame = json.dumps({"pdf_path": request.path, "page": request.page}).encode("ascii") + b"\n"
    deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(DEADLINE_SECONDS)
            peer.connect(str(socket_path))
            peer.sendall(frame)
            data = bytearray()
            while len(data) < PDF_REPLY_LIMIT:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                peer.settimeout(remaining)
                part = peer.recv(min(65536, PDF_REPLY_LIMIT - len(data)))
                if not part:
                    break
                data.extend(part)
                if b"\n" in part:
                    break
    except OSError:
        raise ValueError("The read-only PDF renderer is unavailable or interrupted.") from None
    try:
        if not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise ValueError
        body = json.loads(data)
        if not isinstance(body, dict):
            raise ValueError
        if set(body) == {"error"} and isinstance(body["error"], str) and 0 < len(body["error"]) <= 250:
            error = body["error"]
        else:
            if (set(body) != {"png", "page_count"} or type(body["page_count"]) is not int
                    or not page <= body["page_count"] <= 999999999):
                raise ValueError
            image = parse_image({"mime_type": "image/png", "data": body["png"]})
            return image, body["page_count"]
    except (ValueError, RecursionError):
        raise ValueError("Invalid or incomplete PDF renderer response.") from None
    raise ValueError(error)
