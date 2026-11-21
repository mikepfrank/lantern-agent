"""Persistent credentials for the private room's single operator."""

from contextlib import closing, contextmanager
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
from threading import BoundedSemaphore
import time


SESSION_SECONDS = 30 * 24 * 60 * 60
ATTEMPT_WINDOW = 5 * 60
MAX_ATTEMPTS = 8
# scrypt uses about 32 MiB per verification. Bound public requests across threads.
_hash_slot = BoundedSemaphore(1)


class LoginThrottled(Exception):
    def __init__(self, retry_after: int):
        super().__init__("Please wait before signing in again.")
        self.retry_after = retry_after


def _password_bytes(password):
    if not isinstance(password, str) or not 15 <= len(password) <= 256:
        return None
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _password_digest(password: bytes, salt: bytes):
    # Same scrypt parameters as the chess service. Each installation has its own salt.
    return hashlib.scrypt(password, salt=salt, n=32768, r=8, p=3,
                          maxmem=64 * 1024 * 1024, dklen=64)


def _session_digest(token):
    if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return hashlib.sha256(token.encode("ascii")).digest()
    return None


class WebAuth:
    def __init__(self, state: Path, *, clock=time.time):
        self.clock = clock
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = state / "web-auth.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operator (
                    id INTEGER PRIMARY KEY CHECK (id=1), salt BLOB, password_digest BLOB,
                    epoch INTEGER NOT NULL DEFAULT 0, window_start REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO operator(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS sessions (
                    digest BLOB PRIMARY KEY, expires REAL NOT NULL);
            """)

    @contextmanager
    def _db(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def set_password(self, password: str) -> None:
        encoded = _password_bytes(password)
        if encoded is None:
            raise ValueError("Use a password of 15 to 256 characters.")
        salt = secrets.token_bytes(16)
        digest = _password_digest(encoded, salt)
        with self._db(write=True) as db:
            db.execute("""UPDATE operator SET salt=?, password_digest=?, epoch=epoch+1,
                attempts=0, window_start=0 WHERE id=1""", (salt, digest))
            db.execute("DELETE FROM sessions")

    def sign_in(self, password: str) -> str | None:
        encoded = _password_bytes(password)
        if encoded is None:
            return None
        if not _hash_slot.acquire(blocking=False):
            raise LoginThrottled(1)
        try:
            with self._db(write=True) as db:
                salt, expected, epoch, start, attempts = db.execute("""SELECT salt, password_digest,
                    epoch, window_start, attempts FROM operator WHERE id=1""").fetchone()
                if salt is None:
                    return None
                now = self.clock()
                if now < start or now >= start + ATTEMPT_WINDOW or attempts == 0:
                    start, attempts = now, 0
                if attempts >= MAX_ATTEMPTS:
                    raise LoginThrottled(max(1, math.ceil(start + ATTEMPT_WINDOW - now)))
                # Count before doing expensive work, including across web restarts.
                db.execute("UPDATE operator SET window_start=?, attempts=? WHERE id=1", (start, attempts + 1))
            if not hmac.compare_digest(_password_digest(encoded, salt), expected):
                return None
            with self._db(write=True) as db:
                # Password rotation or revocation while hashing takes precedence.
                if db.execute("UPDATE operator SET attempts=0 WHERE id=1 AND epoch=?", (epoch,)).rowcount != 1:
                    return None
                now = self.clock()
                token = secrets.token_urlsafe(32)
                db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
                db.execute("INSERT INTO sessions(digest,expires) VALUES (?,?)",
                           (_session_digest(token), now + SESSION_SECONDS))
                return token
        finally:
            _hash_slot.release()

    def valid_session(self, token: str) -> bool:
        digest = _session_digest(token)
        if digest is None:
            return False
        with self._db() as db:
            return db.execute("SELECT 1 FROM sessions WHERE digest=? AND expires>?",
                              (digest, self.clock())).fetchone() is not None

    def sign_out(self, token: str) -> None:
        digest = _session_digest(token)
        if digest is not None:
            with self._db(write=True) as db:
                db.execute("DELETE FROM sessions WHERE digest=?", (digest,))

    def revoke_all(self) -> int:
        with self._db(write=True) as db:
            db.execute("UPDATE operator SET epoch=epoch+1 WHERE id=1")
            return db.execute("DELETE FROM sessions").rowcount
