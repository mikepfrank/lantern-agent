"""Private-room authority for inspecting specific third-party Git repositories."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3

from .clone_fetch import normalize_repository, resolve_branch, validate_branch


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Use the numeric repository request or grant ID from the room.")
    return value


def _commit(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{40}", value):
        raise ValueError("GitHub did not identify a valid commit.")
    return value


def _entry(row):
    result = dict(row)
    result["commit"] = result.pop("tip")
    return result


def checkout_path(repository, commit):
    return f"/work/repos/{repository}/{commit}"


class CloneGrants:
    def __init__(self, state):
        self.path = state / "repository-clones.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS grants (
                    id INTEGER PRIMARY KEY, repository TEXT NOT NULL, repository_id INTEGER NOT NULL,
                    branch TEXT NOT NULL, tip TEXT NOT NULL, purpose TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', mode TEXT,
                    requested_at TEXT NOT NULL, decided_at TEXT, revoked_at TEXT);
                CREATE INDEX IF NOT EXISTS clone_scope ON grants(repository_id,repository,branch,id);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY, grant_id INTEGER NOT NULL REFERENCES grants(id),
                    tip TEXT NOT NULL, previous_commit TEXT, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'prepared', delivered_at TEXT);
                CREATE INDEX IF NOT EXISTS clone_deliveries ON deliveries(grant_id,id);
                CREATE TABLE IF NOT EXISTS calls (
                    key TEXT PRIMARY KEY, delivery_id INTEGER NOT NULL REFERENCES deliveries(id));
            """)

    @contextmanager
    def connect(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=60)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def propose(self, repository, purpose, branch=None):
        repository = normalize_repository(repository)
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 400 or not purpose.isprintable():
            raise ValueError("Describe the reason for studying this repository in 1–400 printable characters.")
        remote = resolve_branch(repository, None if branch == "" else branch)
        return self._request(repository, _id(remote["repository_id"]), validate_branch(remote["branch"]),
                             _commit(remote["commit"]), purpose.strip())

    def _request(self, repository, repo_id, branch, tip, purpose, *, reopen=False):
        with self.connect(write=True) as db:
            # An open standing grant covers *this* GitHub repository identity
            # and branch. A one-time decision covers only its frozen commit.
            scope = (repo_id, repository, branch)
            latest = db.execute("""SELECT * FROM grants WHERE repository_id=? AND repository=? AND branch=?
                ORDER BY id DESC LIMIT 1""", scope).fetchone()
            if latest:
                if latest["status"] == "pending" or (latest["mode"], latest["status"]) == ("standing", "approved"):
                    if reopen and (latest["tip"] != tip or latest["status"] != "pending"):
                        raise ValueError("This branch already has a different active request or grant. Review it in the room.")
                    return _entry(latest)
                if not reopen and latest["status"] in ("revoked", "denied"):
                    return _entry(latest)
            if not reopen:
                known = db.execute("""SELECT * FROM grants WHERE repository_id=? AND repository=? AND branch=?
                    AND tip=? ORDER BY id DESC LIMIT 1""", (*scope, tip)).fetchone()
                if known:
                    return _entry(known)
            if db.execute("SELECT count(*) FROM grants WHERE status='pending'").fetchone()[0] >= 16:
                raise ValueError("The room has 16 pending repository requests. Resolve one before adding another.")
            number = db.execute("""INSERT INTO grants(repository,repository_id,branch,tip,purpose,requested_at)
                VALUES(?,?,?,?,?,?)""", (repository, repo_id, branch, tip, purpose, _now())).lastrowid
            return _entry(db.execute("SELECT * FROM grants WHERE id=?", (number,)).fetchone())

    def reopen(self, request_id):
        row = self.status(request_id)
        if row["status"] not in ("denied", "revoked"):
            raise ValueError("Only a denied or revoked repository request can be reconsidered.")
        return self._request(row["repository"], row["repository_id"], row["branch"], row["commit"],
                             row["purpose"], reopen=True)

    def decide(self, request_id, decision, *, room=None):
        modes = {"allow_once": "once", "allow_standing": "standing", "deny": None}
        if not isinstance(decision, str) or decision not in modes:
            raise ValueError("Choose allow once, allow branch updates, or deny.")
        status, mode = ("denied" if decision == "deny" else "approved"), modes[decision]
        with self.connect(write=True) as db:
            row = db.execute("SELECT status,mode,repository,branch,tip FROM grants WHERE id=?",
                             (_id(request_id),)).fetchone()
            if not row:
                raise ValueError("Unknown repository request.")
            if row["status"] == "pending":
                db.execute("UPDATE grants SET status=?,mode=?,decided_at=? WHERE id=?",
                           (status, mode, _now(), request_id))
                if room is not None and status == "approved":
                    branch = json.dumps(row["branch"], ensure_ascii=False)
                    scope = "this commit only" if mode == "once" else "standing permission for future updates on this branch"
                    text = (f"The operator approved repository clone grant #{request_id} for {row['repository']} "
                            f"on branch {branch} at {row['tip']} ({scope}). Use clone_repository with grant_id {request_id}.")
                    room.record_import_approval(db, "repository", request_id, mode, text)
            elif (row["status"], row["mode"]) != (status, mode):
                raise ValueError("This request already has a decision. Refresh the room.")
            return {"id": request_id, "status": status, "mode": mode}

    def revoke(self, grant_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT status FROM grants WHERE id=?", (_id(grant_id),)).fetchone()
            if row is None or row["status"] not in ("approved", "revoked"):
                raise ValueError("Only an active repository grant can be revoked.")
            if row["status"] == "approved":
                db.execute("UPDATE grants SET status='revoked',revoked_at=? WHERE id=?", (_now(), grant_id))
            return {"id": grant_id, "status": "revoked"}

    @staticmethod
    def receipt(row, scope):
        return {"grant_id": scope["id"], "repository": scope["repository"], "branch": scope["branch"],
                "commit": row["tip"], "previous_commit": row["previous_commit"], "size_bytes": row["size_bytes"],
                "path": checkout_path(scope["repository"], row["tip"]), "delivered_at": row["delivered_at"]}

    def _receipts(self, db, grant_id=None):
        rows = db.execute("""SELECT d.*,g.repository,g.branch FROM deliveries d JOIN grants g ON g.id=d.grant_id
            WHERE d.status='delivered' AND (? IS NULL OR d.grant_id=?) ORDER BY d.id DESC LIMIT 25""",
                          (grant_id, grant_id))
        return [self.receipt(r, {"id": r["grant_id"], "repository": r["repository"], "branch": r["branch"]})
                for r in rows]

    def overview(self):
        with self.connect() as db:
            return {"requests": [_entry(r) for r in db.execute("SELECT * FROM grants WHERE status='pending' ORDER BY id DESC")],
                    "grants": [_entry(r) for r in db.execute("SELECT * FROM grants WHERE status='approved' ORDER BY id DESC")],
                    "closed": [_entry(r) for r in db.execute(
                        "SELECT * FROM grants WHERE status IN ('denied','revoked') ORDER BY id DESC LIMIT 10")],
                    "receipts": self._receipts(db)}

    def status(self, request_id=None):
        with self.connect() as db:
            if request_id is None:
                return {"recent_requests": [_entry(r) for r in db.execute("SELECT * FROM grants ORDER BY id DESC LIMIT 40")]}
            row = db.execute("SELECT * FROM grants WHERE id=?", (_id(request_id),)).fetchone()
            if row is None:
                raise ValueError("Unknown repository request.")
            return {**_entry(row), "receipts": self._receipts(db, request_id)}

    @staticmethod
    def scope(db, grant_id):
        row = db.execute("SELECT * FROM grants WHERE id=?", (_id(grant_id),)).fetchone()
        if row is None or row["status"] not in ("approved", "fulfilled"):
            raise ValueError("This clone requires an approved repository grant. Check repository_clone_status.")
        return _entry(row)

    @staticmethod
    def prior(db, scope, call_key):
        exact = db.execute("""SELECT d.* FROM calls c JOIN deliveries d ON d.id=c.delivery_id
                              WHERE c.key=?""", (call_key,)).fetchone()
        if exact:
            if exact["grant_id"] != scope["id"]:
                raise ValueError("This call ID belongs to a different repository grant.")
            return exact
        return db.execute("""SELECT * FROM deliveries WHERE grant_id=?
            AND (status='prepared' OR (status='delivered' AND ?='once')) ORDER BY id DESC LIMIT 1""",
                          (scope["id"], scope["mode"])).fetchone()

    def evidence(self, args, call_key):
        if set(args) != {"grant_id"}:
            raise ValueError("Supply only the numeric clone grant_id.")
        with self.connect() as db:
            grant = self.scope(db, args["grant_id"])
            prior = self.prior(db, grant, call_key)
            previous = db.execute("""SELECT tip FROM deliveries WHERE grant_id=? AND status='delivered'
                ORDER BY id DESC LIMIT 1""", (grant["id"],)).fetchone()
        target = prior["tip"] if prior else grant["commit"]
        # The first checkout uses the revision shown to the operator. Later
        # standing updates resolve the original branch, then freeze it for review.
        if grant["mode"] == "standing" and previous and not prior:
            remote = resolve_branch(grant["repository"], grant["branch"])
            if remote["repository_id"] != grant["repository_id"] or remote["branch"] != grant["branch"]:
                raise ValueError("This repository identity or branch changed. Request a fresh grant.")
            target = _commit(remote["commit"])
        return {"grant_id": grant["id"], "repository": grant["repository"], "repository_id": grant["repository_id"],
                "branch": grant["branch"], "mode": grant["mode"], "decided_at": grant["decided_at"],
                "approved_commit": grant["commit"], "target_commit": target,
                "previous_commit": previous["tip"] if previous else None,
                "destination": checkout_path(grant["repository"], target)}

    def pending_objects(self):
        with self.connect() as db:
            return {r[0] for r in db.execute("""SELECT DISTINCT d.sha256 FROM deliveries d JOIN grants g
                ON d.grant_id=g.id WHERE d.status='prepared' AND g.status='approved'""")}
