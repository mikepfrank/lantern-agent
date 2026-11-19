from contextlib import contextmanager
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lantern import chat_cli
from lantern.chat_store import ChatStore


class TTY(StringIO):
    def isatty(self):
        return True


class ChatCLITests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = ChatStore(self.state)

    def test_reconnect_replays_backlog_across_pages(self):
        self.store.submit("initial message")
        dest = StringIO()
        chat_cli.attach(self.store, input_stream=StringIO("/quit\n"), output_stream=dest)
        initial = self.store.read_cursor()
        for number in range(110):
            self.store.submit(f"offline event [{number}]")
        dest = StringIO()
        chat_cli.attach(self.store, input_stream=StringIO("follow-up\n/quit\nignored\n"), output_stream=dest)
        self.assertIn("offline event [0]", dest.getvalue())
        self.assertIn("offline event [109]", dest.getvalue())
        self.assertIn("follow-up", dest.getvalue())
        self.assertNotIn("ignored", dest.getvalue())
        self.assertGreater(self.store.read_cursor(), initial)

    def test_control_text_cannot_issue_terminal_commands(self):
        self.store.submit("Move\x1b]0;change-title\x07 done")
        dest = StringIO()
        chat_cli.attach(self.store, input_stream=StringIO("/status\n/quit\n"), output_stream=dest)
        self.assertNotIn("\x1b", dest.getvalue())
        self.assertNotIn("\x07", dest.getvalue())
        self.assertIn("Queued messages: 1", dest.getvalue())

    def test_reply_during_edit_is_printed_and_draft_is_preserved(self):
        self.store.submit("start work")
        batch = self.store.claim()

        def actions():
            yield ["H", "i", "left"]
            self.store.notify(batch.id, "An incoming event", "test-tool-call")
            yield []
            yield ["a", "end", "enter", "quit"]

        @contextmanager
        def keys(_stream):
            iterator = actions()
            yield lambda: next(iterator)

        dest = TTY()
        with patch.dict(os.environ, TERM="xterm"), patch.object(chat_cli, "_terminal_keys", keys), \
             patch.object(chat_cli.time, "monotonic", side_effect=range(100)):
            chat_cli.attach(self.store, input_stream=TTY(), output_stream=dest)
        self.assertIn("An incoming event", dest.getvalue())
        self.assertIn("You> H\x1b[si", dest.getvalue().split("An incoming event", 1)[1])
        self.assertEqual([e.text for e in self.store.recent(2)], ["An incoming event", "Hai"])

    @unittest.skipUnless(os.name == "posix", "requires select on a pipe")
    def test_ssh_pipe_receives_event_without_another_enter(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        ready, received = Event(), Event()

        class Capture(StringIO):
            def write(self, text):
                if chat_cli.HELP in text:
                    ready.set()
                if "background reply" in text:
                    received.set()
                return super().write(text)

        self.store.submit("start work")
        batch = self.store.claim()
        reader, writer = os.pipe()
        with os.fdopen(reader, encoding="utf-8") as source, ThreadPoolExecutor(max_workers=1) as pool:
            run = pool.submit(chat_cli.attach, self.store, input_stream=source, output_stream=Capture())
            try:
                self.assertTrue(ready.wait(3))
                self.store.notify(batch.id, "background reply", "notification")
                self.assertTrue(received.wait(3))
            finally:
                os.close(writer)
            run.result(timeout=3)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX pseudo-terminal")
    def test_real_ssh_style_tty(self):
        import pty
        import select
        import subprocess
        import termios

        primary, secondary = pty.openpty()
        self.addCleanup(os.close, primary)
        self.addCleanup(os.close, secondary)
        previous = termios.tcgetattr(secondary)
        script = ("from pathlib import Path; import sys; from lantern.chat_cli import attach; "
                  "from lantern.chat_store import ChatStore; attach(ChatStore(Path(sys.argv[1])))")
        with subprocess.Popen([sys.executable, "-c", script, str(self.state)], stdin=secondary,
                              stdout=secondary, stderr=secondary, env={**os.environ, "TERM": "xterm"}) as child:
            def read_until(expected):
                received = b""
                deadline = time.monotonic() + 5
                while expected not in received:
                    if not select.select([primary], [], [], max(0, deadline - time.monotonic()))[0]:
                        self.fail(f"Missing {expected!r} in {received[-200:]!r}")
                    received += os.read(primary, 4096)

            try:
                read_until(b"You> ")
                os.write(primary, b"compose")
                read_until(b"You> compose")
                self.store.submit("another session sent a message")
                read_until(b"another session sent a message")
                os.write(primary, b"d\r")
                read_until(b"composed")
                os.write(primary, b"/quit\r")
                self.assertEqual(child.wait(timeout=5), 0)
            finally:
                if child.poll() is None:
                    child.kill()
        self.assertEqual([e.text for e in self.store.recent(2)], ["another session sent a message", "composed"])
        self.assertEqual(termios.tcgetattr(secondary), previous)
