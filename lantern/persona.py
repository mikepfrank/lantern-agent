"""Pin the operator-supplied persona text to its Codex thread."""

from contextlib import closing
from importlib.resources import files
from pathlib import Path
import sqlite3


def default_persona() -> str:
    return files("lantern").joinpath("personas", "lantern-v1.md").read_text(encoding="utf-8")


class ThreadPersonas:
    def __init__(self, state: Path):
        self.path = state / "threads.sqlite"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS personas (thread TEXT PRIMARY KEY, text TEXT NOT NULL)")

    def select(self, thread: str | None, supplied: str | None) -> str:
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
