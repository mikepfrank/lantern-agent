"""An attachable terminal for the operator's durable Lantern chat."""

import codecs
from contextlib import contextmanager
from io import UnsupportedOperation
import os
import select
import shutil
import sys
import time
from unicodedata import combining, east_asian_width


HELP = "Enter sends a message. /status shows queued work, /history shows the last 20 events, and /quit detaches."
PROMPT = "You> "
POLL_SECONDS = 0.20


def _display_text(text):
    # Preserve newlines and Unicode while keeping model-supplied terminal
    # controls out of the operator's terminal.
    text = text.replace("\r\n", "\n").replace("\t", "    ")
    return "".join(c for c in text if c == "\n" or (ord(c) >= 32 and not 127 <= ord(c) <= 159))


def _event_line(event):
    speaker = {"operator": "You", "lantern": "Lantern", "system": "Status"}[event.role]
    timestamp = event.at[:16].replace("T", " ")
    body = _display_text(event.text).replace("\n", "\n    ")
    return f"[{timestamp} UTC] {speaker} #{event.id}  {body}"


class _Chat:
    def __init__(self, store, write_line):
        self.store = store
        self.write_line = write_line
        self.cursor = store.read_cursor() or 0

    def welcome(self):
        self.write_line(HELP)
        if self.cursor:
            for event in self.store.recent(8):
                if event.id <= self.cursor:
                    self.write_line(_event_line(event))
        self.poll()

    def poll(self):
        while True:
            events = self.store.events(after=self.cursor, limit=100)
            if not events:
                return
            for event in events:
                self.write_line(_event_line(event))
            # Mark only after the batch has reached the output stream. If the
            # terminal disconnects while printing, the batch can be read again.
            self.cursor = events[-1].id
            self.store.mark_read(self.cursor)
            if len(events) < 100:
                return

    def line(self, line):
        command = line.strip()
        if command == "/quit":
            return False
        if command == "/help":
            self.write_line(HELP)
        elif command == "/history":
            for event in self.store.recent(20):
                self.write_line(_event_line(event))
        elif command == "/status":
            status = self.store.status()
            active = status["active_batch"]
            current = f"Working on batch {active}." if active is not None else "No active turn."
            self.write_line(f"{current} Queued messages: {status['queued']}.")
        elif command:
            try:
                self.store.submit(line)
            except ValueError as error:
                self.write_line(_display_text(str(error)))
        self.poll()
        return True


def _cell_width(character):
    if combining(character):
        return 0
    return 2 if east_asian_width(character) in ("W", "F") else 1


class _Draft:
    """Single-line editor. Keep the caret on one physical row during redraws."""

    def __init__(self):
        self.left = []
        self.right = []  # reversed, with the character after the caret at the end

    def render(self, columns):
        available = max(columns - len(PROMPT) - 1, 0)
        left, right = [], []
        for c in reversed(self.left):
            width = _cell_width(c)
            if width > available:
                break
            available -= width
            left.append(c)
        for c in reversed(self.right):
            width = _cell_width(c)
            if width > available:
                break
            available -= width
            right.append(c)
        return PROMPT + "".join(reversed(left)) + "\x1b[s" + "".join(right) + "\x1b[u"

    def edit(self, key):
        if key == "enter":
            line = "".join(self.left) + "".join(reversed(self.right))
            self.left.clear()
            self.right.clear()
            return line
        if key in ("left", "right"):
            source, dest = (self.left, self.right) if key == "left" else (self.right, self.left)
            if source:
                dest.append(source.pop())
        elif key in ("home", "end"):
            source, dest = (self.left, self.right) if key == "home" else (self.right, self.left)
            dest.extend(reversed(source))
            source.clear()
        elif key == "backspace" and self.left:
            self.left.pop()
        elif key == "delete" and self.right:
            self.right.pop()
        elif key == "clear":
            self.left.clear()
        elif len(key) == 1 and key.isprintable():
            self.left.append(key)
        return None


CONTROL_KEYS = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\b": "backspace",
                "\x04": "eof", "\x03": "quit", "\x01": "home", "\x05": "end", "\x15": "clear"}
ESC_KEYS = {"[D": "left", "[C": "right", "[H": "home", "[F": "end", "[3~": "delete",
            "[1~": "home", "[4~": "end", "OD": "left", "OC": "right", "OH": "home", "OF": "end"}


@contextmanager
def _terminal_keys(stream):
    if os.name == "nt":
        import msvcrt
        # The Windows SSH client presents a POSIX TTY on the host. This path
        # also supports attaching with a modern terminal directly on Windows.
        def read():
            if not msvcrt.kbhit():
                time.sleep(0.04)
                return []
            c = msvcrt.getwch()
            if c in ("\x00", "\xe0"):
                key = {"K": "left", "M": "right", "G": "home", "O": "end",
                       "S": "delete"}.get(msvcrt.getwch())
                return [key] if key else []
            if 0xD800 <= ord(c) <= 0xDBFF:
                c = (c + msvcrt.getwch()).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
            return [CONTROL_KEYS.get(c, c)]

        yield read
        return

    import termios
    import tty

    fd = stream.fileno()
    previous = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder(stream.encoding or "utf-8")(errors="replace")
    escape = ""

    def read():
        nonlocal escape
        if not select.select([fd], [], [], POLL_SECONDS)[0]:
            escape = ""  # discard a lone escape
            return []
        data = os.read(fd, 4096)
        if not data:
            return ["quit"]
        keys = []
        for c in decoder.decode(data):
            if c == "\x1b":
                escape = c
            elif escape:
                escape += c
                sequence = escape[1:]
                if sequence in ESC_KEYS:
                    keys.append(ESC_KEYS[sequence])
                    escape = ""
                elif sequence not in ("[", "O") and (c.isalpha() or c == "~" or len(escape) >= 16):
                    escape = ""
            else:
                keys.append(CONTROL_KEYS.get(c, c))
        return keys

    try:
        tty.setcbreak(fd, termios.TCSANOW)
        yield read
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)


@contextmanager
def _screen_mode(source, dest):
    enabled = source.isatty() and dest.isatty() and os.environ.get("TERM") != "dumb"
    if not enabled or os.name != "nt" or dest is not sys.stdout:
        yield enabled
        return

    # Some Windows console hosts leave VT processing disabled for Python.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    console = ctypes.windll.kernel32
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(dest.fileno()))
    original = wintypes.DWORD()
    if not console.GetConsoleMode(handle, ctypes.byref(original)):
        yield False
        return
    enabled = bool(console.SetConsoleMode(handle, original.value | 0x0004))
    try:
        yield enabled
    finally:
        if enabled:
            console.SetConsoleMode(handle, original.value)


def attach(store, *, input_stream=None, output_stream=None):
    """Read input and poll the durable conversation until EOF or /quit."""
    source = input_stream if input_stream is not None else sys.stdin
    dest = output_stream if output_stream is not None else sys.stdout
    with _screen_mode(source, dest) as interactive:
        _attach(store, source, dest, interactive)


def _line_mode(chat, source):
    # SSH without a PTY uses pipes. Keep watching for replies even while its
    # input pipe stays open and the operator has not sent another line.
    try:
        fd = source.fileno()
    except (UnsupportedOperation, AttributeError):
        fd = None
    if os.name != "posix" or fd is None:
        for line in source:
            if not chat.line(line.rstrip("\r\n")):
                return
        return

    decoder = codecs.getincrementaldecoder(source.encoding or "utf-8")(errors="replace")
    pending = ""
    while True:
        chat.poll()
        if not select.select([fd], [], [], POLL_SECONDS)[0]:
            continue
        data = os.read(fd, 4096)
        pending += decoder.decode(data, final=not data)
        lines = pending.split("\n")
        pending = lines.pop()
        for line in lines:
            if not chat.line(line.rstrip("\r")):
                return
        if not data:
            if pending:
                chat.line(pending.rstrip("\r"))
            return


def _attach(store, source, dest, interactive):
    erase = "\r\x1b[2K" if interactive else ""

    def write_line(line):
        dest.write(erase + line + "\n")
        dest.flush()

    chat = _Chat(store, write_line)
    chat.welcome()
    if not interactive:
        _line_mode(chat, source)
        return

    draft = _Draft()
    deadline = 0
    drawn = None
    try:
        with _terminal_keys(source) as read_keys:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    old = chat.cursor
                    chat.poll()
                    if chat.cursor != old:
                        drawn = None
                    deadline = now + POLL_SECONDS
                visible = draft.render(shutil.get_terminal_size().columns)
                if drawn != visible:
                    dest.write(erase + visible)
                    dest.flush()
                    drawn = visible
                for key in read_keys():
                    if key == "quit" or (key == "eof" and not (draft.left or draft.right)):
                        return
                    line = draft.edit("delete" if key == "eof" else key)
                    if line is not None:
                        if not chat.line(line):
                            return
                        drawn = None
    except KeyboardInterrupt:
        pass
    finally:
        dest.write(erase)
        dest.flush()
