import base64
from contextlib import closing
from pathlib import Path
import sqlite3
import struct
from tempfile import TemporaryDirectory
import unittest
import zlib

from lantern.attachments import MAX_IMAGE_BYTES, SIGNATURE, parse_image
from lantern.chat_store import ChatStore
from lantern.chat_cli import _event_line


def chunk(kind, data=b""):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(*, header=None, pixels=b"\0\x31\x72\xba", extra=b""):
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0) if header is None else header
    return SIGNATURE + chunk(b"IHDR", ihdr) + extra + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND")


def upload(data):
    return {"mime_type": "image/png", "data": base64.b64encode(data).decode("ascii")}


class AttachmentTests(unittest.TestCase):
    def test_scanline_filters_across_decoder_chunks(self):
        width, height = 1600, 1600
        row = b"\x01" + bytes(width * 4)
        header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        image = parse_image(upload(png(header=header, pixels=row * height)))
        self.assertEqual((width, height), (image.width, image.height))
        last_bad_filter = row * (height - 1) + b"\x06" + row[1:]
        with self.assertRaisesRegex(ValueError, "pixel layout"):
            parse_image(upload(png(header=header, pixels=last_bad_filter)))

    def test_validate_raster_and_strip_comments(self):
        bare = png()
        annotated = png(extra=chunk(b"tEXt", b"Comment\0private metadata"))
        self.assertEqual(parse_image(upload(bare)), parse_image(upload(annotated)))
        self.assertEqual(bare, parse_image(upload(annotated)).data)
        bad_header = struct.pack(">IIBBBBB", 1601, 1, 8, 2, 0, 0, 0)
        bad = [b"<svg/>", png(header=bad_header), bare[:-1], bare + b"garbage",
               bare[:25] + b"B" + bare[26:], png(pixels=b"\x09abc"), png(pixels=b"abc"),
               png(pixels=b"\0" * (4 * 1024 * 1024)), b"X" * (MAX_IMAGE_BYTES + 1),
               png(extra=chunk(b"acTL", b"\0" * 8))]
        for data in bad:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                parse_image(upload(data))
        for body in ({}, None, {"mime_type": "image/png", "data": "https://example.org/test.png"},
                     {**upload(bare), "mime_type": "text/html"}):
            with self.assertRaises(ValueError):
                parse_image(body)

    def test_preexisting_room_and_image_retries(self):
        state = Path(self.enterContext(TemporaryDirectory()))
        with closing(sqlite3.connect(state / "chat.sqlite")) as db, db:
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,role TEXT,text TEXT,at TEXT,client_id TEXT UNIQUE)")
            db.execute("INSERT INTO events VALUES(1,'operator','original','date','old')")
        store = ChatStore(state)
        self.assertIsNone(store.recent()[0].image)
        red = parse_image(upload(png()))
        different = parse_image(upload(png(pixels=b"\0xxx")))
        event_id = store.submit("", "image-key", image=red)
        self.assertEqual(event_id, ChatStore(state).submit("", "image-key", image=red))
        for text, image in (("caption", red), ("", different), ("text only", None)):
            with self.assertRaises(ValueError):
                store.submit(text, "image-key", image=image)
        event, = store.claim().messages
        self.assertEqual((event_id, red.sha256, len(red.data)), (event.id, event.image["sha256"], event.image["size"]))
        self.assertEqual(red.data, store.image_data(event.id))
        self.assertIn("[Image 1 × 1]", _event_line(event))
        self.assertIsNone(store.image_data(1))
        self.assertEqual(2, len(store.events()))
        store.submit("What color?", image=red)
        self.assertIn("[Image 1 × 1]\n    What color?", _event_line(store.recent(1)[0]))

    def test_image_overflow_and_later_text_stay_in_order_across_steer(self):
        state = Path(self.enterContext(TemporaryDirectory()))
        store = ChatStore(state)
        image = parse_image(upload(png()))
        first = [store.submit("initial", image=image) for _ in range(4)]
        fifth = store.submit("five", image=image)
        sixth = store.submit("after five")
        batch = store.claim()
        self.assertEqual(first, [m.id for m in batch.messages])
        self.assertEqual((), store.claim_steer(batch.id))
        store.complete(batch.id, "Done")
        batch = store.claim()
        self.assertEqual([fifth, sixth], [m.id for m in batch.messages])
        claimed = [store.submit("steered", image=image) for _ in range(3)]
        claimed.append(store.submit("allowed before fifth"))
        held = store.submit("deferred image", image=image)
        last = store.submit("after deferred")
        self.assertEqual(claimed, [m.id for m in store.claim_steer(batch.id)])
        store.release_steer(batch.id, claimed)
        self.assertEqual(claimed, [m.id for m in store.claim_steer(batch.id)])
        store.complete(batch.id, "Done")
        self.assertEqual([held, last], [m.id for m in store.claim().messages])
