from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern import web_auth
from lantern.web_auth import ATTEMPT_WINDOW, LoginThrottled, MAX_ATTEMPTS, SESSION_SECONDS, WebAuth


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory()))
        self.now = 1900000000
        self.auth = WebAuth(self.state, clock=lambda: self.now)
        self.password = "Local fixture passphrase."

    def test_sessions_survive_restart_and_expire_even_with_regular_use(self):
        self.assertIsNone(self.auth.sign_in(self.password))
        self.auth.set_password(self.password)
        token = self.auth.sign_in(self.password)
        restarted = WebAuth(self.state, clock=lambda: self.now)
        self.assertTrue(restarted.valid_session(token))
        with closing(sqlite3.connect(self.auth.path)) as db:
            digest, expires = db.execute("SELECT digest, expires FROM sessions").fetchone()
            self.assertEqual(digest, hashlib.sha256(token.encode("ascii")).digest())
            self.assertEqual(expires, self.now + SESSION_SECONDS)
        self.assertNotIn(self.password.encode(), self.auth.path.read_bytes())
        self.assertNotIn(token.encode(), self.auth.path.read_bytes())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.auth.path.stat().st_mode), 0o600)
        self.now += SESSION_SECONDS - 1
        self.assertTrue(restarted.valid_session(token))
        self.now += 1
        self.assertFalse(restarted.valid_session(token))

    def test_logout_rotation_and_revocation(self):
        self.auth.set_password(self.password)
        first = self.auth.sign_in(self.password)
        second = self.auth.sign_in(self.password)
        self.assertNotEqual(first, second)
        self.auth.sign_out(first)
        self.assertFalse(self.auth.valid_session(first))
        self.assertTrue(self.auth.valid_session(second))
        new_password = self.password + " changed"
        self.auth.set_password(new_password)
        self.assertFalse(self.auth.valid_session(second))
        self.assertIsNone(self.auth.sign_in(self.password))
        third = self.auth.sign_in(new_password)
        self.assertEqual(self.auth.revoke_all(), 1)
        self.assertFalse(self.auth.valid_session(third))
        self.assertTrue(self.auth.valid_session(self.auth.sign_in(new_password)))

    def test_rate_limit_survives_restart_and_expires(self):
        self.auth.set_password(self.password)
        with patch("lantern.web_auth._password_digest", return_value=b"invalid"):
            for _ in range(MAX_ATTEMPTS):
                self.assertIsNone(self.auth.sign_in(self.password + "wrong"))
        restarted = WebAuth(self.state, clock=lambda: self.now)
        self.now += 2
        with self.assertRaises(LoginThrottled) as error:
            restarted.sign_in(self.password)
        self.assertEqual(error.exception.retry_after, ATTEMPT_WINDOW - 2)
        self.now += ATTEMPT_WINDOW - 2
        self.assertTrue(restarted.valid_session(restarted.sign_in(self.password)))

    def test_revocation_during_password_check_cannot_issue_session(self):
        self.auth.set_password(self.password)
        original_digest = web_auth._password_digest

        def revoke_while_hashing(encoded, salt):
            with self.assertRaises(LoginThrottled):
                self.auth.sign_in(self.password)
            self.auth.revoke_all()
            return original_digest(encoded, salt)

        with patch("lantern.web_auth._password_digest", side_effect=revoke_while_hashing):
            self.assertIsNone(self.auth.sign_in(self.password))
        self.assertTrue(self.auth.valid_session(self.auth.sign_in(self.password)))

    def test_invalid_password_change_leaves_current_session_alive(self):
        self.auth.set_password(self.password)
        token = self.auth.sign_in(self.password)
        for invalid in ("short", "a" * 257, "a" * 15 + "\ud800"):
            with self.assertRaises(ValueError):
                self.auth.set_password(invalid)
            self.assertIsNone(self.auth.sign_in(invalid))
        self.assertTrue(self.auth.valid_session(token))
        for invalid in (None, "", token + "\n"):
            self.assertFalse(self.auth.valid_session(invalid))
