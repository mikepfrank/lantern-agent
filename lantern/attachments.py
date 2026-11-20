"""Validate the bounded, non-interlaced PNG raster sent by the browser canvas."""

import base64
import binascii
from dataclasses import dataclass
import hashlib
import struct
import zlib


MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_SIDE = 1600
MAX_TURN_IMAGES = 4
MAX_IMAGE_BASE64 = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ImageAttachment:
    data: bytes
    width: int
    height: int
    sha256: str


def _validate_pixels(compressed, stride, height):
    # Even a tiny, flat PNG can represent over 10 MB of pixels. Check each
    # scanline's filter byte without keeping the entire raster in worker RAM.
    expected = stride * height
    stream = zlib.decompressobj()
    seen = 0
    block_size = 64 * 1024
    for offset in range(0, len(compressed), block_size):
        part = memoryview(compressed)[offset:offset + block_size]
        while True:
            limit = min(expected - seen + 1, block_size)
            block = stream.decompress(part, limit)
            if seen + len(block) > expected or any(f > 4 for f in block[-seen % stride::stride]):
                raise ValueError("Invalid PNG pixel layout.")
            seen += len(block)
            if stream.unused_data:
                raise ValueError("Trailing compressed PNG data.")
            part = stream.unconsumed_tail
            if not part and (stream.eof or len(block) < limit):
                break
    if seen != expected or not stream.eof:
        raise ValueError("Incomplete PNG pixel data.")


def parse_image(value) -> ImageAttachment:
    if (not isinstance(value, dict) or set(value) != {"mime_type", "data"}
            or value["mime_type"] != "image/png" or not isinstance(value["data"], str)
            or not 0 < len(value["data"]) <= MAX_IMAGE_BASE64):
        raise ValueError("Expected a PNG image of at most 1 MiB.")
    try:
        raw = base64.b64decode(value["data"], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid image encoding.") from None
    if len(raw) > MAX_IMAGE_BYTES or not raw.startswith(SIGNATURE):
        raise ValueError("Invalid PNG image.")

    offset = len(SIGNATURE)
    image = bytearray(SIGNATURE)
    pixels = bytearray()
    channels = {0: 1, 2: 3, 4: 2, 6: 4}
    width = height = color = 0
    transparency = False
    ended = False
    while offset + 12 <= len(raw):
        size, kind = struct.unpack_from(">I4s", raw, offset)
        end = offset + 12 + size
        if end > len(raw) or not kind.isalpha():
            raise ValueError("Invalid PNG chunk.")
        payload = raw[offset + 8:end - 4]
        if zlib.crc32(raw[offset + 4:end - 4]) != int.from_bytes(raw[end - 4:end], "big"):
            raise ValueError("Damaged PNG image.")
        if not width and kind != b"IHDR":
            raise ValueError("Missing PNG header.")
        if kind == b"IHDR":
            if width or size != 13:
                raise ValueError("Invalid PNG header.")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if (not 0 < width <= MAX_IMAGE_SIDE or not 0 < height <= MAX_IMAGE_SIDE or
                    depth != 8 or color not in channels or compression or filtering or interlace):
                raise ValueError("Expected an 8-bit PNG raster up to 1600 pixels per side.")
        elif kind == b"IDAT":
            pixels.extend(payload)
        elif kind == b"IEND":
            if size or end != len(raw):
                raise ValueError("Invalid end of PNG image.")
            ended = True
        elif kind == b"tRNS":
            if transparency or pixels or size != {0: 2, 2: 6}.get(color):
                raise ValueError("Invalid PNG transparency.")
            transparency = True
        elif kind in (b"acTL", b"fcTL", b"fdAT") or (kind[:1].isupper() and kind != b"PLTE"):
            raise ValueError("Unsupported PNG format.")
        # Retain raster data and transparency only. Profiles, comments, and
        # other metadata have no place in the private model input or preview.
        if kind in (b"IHDR", b"IDAT", b"tRNS", b"IEND"):
            image.extend(raw[offset:end])
        offset = end
    if not ended or not pixels:
        raise ValueError("Incomplete PNG image.")
    try:
        _validate_pixels(pixels, 1 + channels[color] * width, height)
    except zlib.error:
        raise ValueError("Invalid compressed PNG pixels.") from None
    data = bytes(image)
    return ImageAttachment(data, width, height, hashlib.sha256(data).hexdigest())
