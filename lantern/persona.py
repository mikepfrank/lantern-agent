"""Pin the operator-supplied persona text to its Codex thread."""

from contextlib import closing
import hashlib
from importlib.resources import files
from pathlib import Path
import sqlite3


def default_persona() -> str:
    return files("lantern").joinpath("personas", "lantern-v1.md").read_text(encoding="utf-8")


class ThreadPersonas:
    def __init__(self, state: Path, *, default_path: Path | None = None):
        self.default_path = default_path
        self.path = state / "threads.sqlite"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS personas (thread TEXT PRIMARY KEY, text TEXT NOT NULL)")

    def select(self, thread: str | None, supplied: str | None) -> str:
        if not thread and supplied is None and self.default_path is not None:
            supplied = self.default_path.read_text(encoding="utf-8-sig")
        if supplied is not None and not supplied.strip():
            raise ValueError("The persona file is empty")
        if thread:
            with closing(sqlite3.connect(self.path)) as db:
                row = db.execute("SELECT text FROM personas WHERE thread=?", (thread,)).fetchone()
            if row is not None:
                if supplied is not None and supplied != row[0]:
                    raise ValueError("This thread already has a different persona. Start a new thread to use the edit.")
                return row[0]
            if supplied is None:
                raise ValueError("No persona is recorded for that thread. Supply its original --persona file.")
        return supplied if supplied is not None else default_persona()

    def bind(self, thread: str, persona: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO personas VALUES (?, ?)", (thread, persona))
            if db.execute("SELECT text FROM personas WHERE thread=?", (thread,)).fetchone() != (persona,):
                raise ValueError("A different persona is already bound to the thread")

    def replace_pin(self, thread: str, expected_sha256: str, replacement_text: str) -> bool:
        """Update one known pin. The digest covers the pinned text encoded as UTF-8."""
        if not replacement_text.strip():
            raise ValueError("The replacement persona is empty")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT text FROM personas WHERE thread=?", (thread,)).fetchone()
            if row is None:
                raise ValueError("No persona is recorded for that thread")
            if row[0] == replacement_text:
                return False
            if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != expected_sha256:
                raise ValueError("The saved persona differs from the expected revision")
            db.execute("UPDATE personas SET text=? WHERE thread=?", (replacement_text, thread))
            return True
