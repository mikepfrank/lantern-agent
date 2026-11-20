"""Small native image items that identify private rasters across Codex replays.

Codex accepts inline images. A one-row PNG stores the event ID and digest as
pixels so the host can restore the original upload at the vendor boundary.
Separate tags distinguish an operator upload from a rendered document page.
"""

import base64
import binascii
import struct
import zlib


PREFIX = "data:image/png;base64,"
TAG = b"Lantern1"
PAGE_TAG = b"LntPage1"


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


# Grayscale row: eight tag bytes, eight event ID bytes and a 32-byte digest.
_START = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 48, 1, 8, 0, 0, 0, 0))
_END = _chunk(b"IEND", b"")


def image_reference(event_id, sha256, *, page=False):
    digest = bytes.fromhex(sha256)
    if type(event_id) is not int or not 0 < event_id < 2**63 or len(digest) != 32:
        raise ValueError("Invalid private image reference")
    row = b"\0" + (PAGE_TAG if page else TAG) + event_id.to_bytes(8, "big") + digest
    png = _START + _chunk(b"IDAT", zlib.compress(row, level=0)) + _END
    return PREFIX + base64.b64encode(png).decode("ascii")


def parse_reference(uri, *, page=False):
    if not isinstance(uri, str) or len(uri) > 256 or not uri.startswith(PREFIX):
        raise ValueError("Expected a private image reference")
    try:
        png = base64.b64decode(uri[len(PREFIX):], validate=True)
        if not png.startswith(_START) or not png.endswith(_END):
            raise ValueError
        start, end = len(_START), len(png) - len(_END)
        if png[start + 4:start + 8] != b"IDAT":
            raise ValueError
        row = zlib.decompressobj().decompress(png[start + 8:end - 4], 50)
        if len(row) != 49 or not row.startswith(b"\0" + (PAGE_TAG if page else TAG)):
            raise ValueError
        event_id = int.from_bytes(row[9:17], "big")
        digest = row[17:].hex()
        if image_reference(event_id, digest, page=page) != uri:
            raise ValueError
    except (ValueError, binascii.Error, zlib.error) as exc:
        raise ValueError("Expected a private image reference") from exc
    return event_id, digest
