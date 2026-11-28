"""Signed-in operator decisions and transfer receipts for the private room."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3

from .import_fetch import MAX_BYTES, checked_filename, checked_import_url


MAX_FILES = 12


def _label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 or not value.isprintable():
        raise ValueError("Give the project and collection short, printable names (up to 80 characters).")
    return value.strip()


def _url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError("Each public source URL must fit in 2048 characters.")
    return checked_import_url(value)


def _manifest(files):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError(f"Name between one and {MAX_FILES} data files in one request.")
    result = []
    names = set()
    for file in files:
        if (not isinstance(file, dict) or not {"filename", "url"} <= file.keys()
                or file.keys() - {"filename", "url", "redirect_urls", "sha256"}):
            raise ValueError("Each file needs filename and url, with optional redirect_urls and sha256.")
        name = checked_filename(file["filename"])
        if name.casefold() in names:
            raise ValueError("Each destination filename in a request must be distinct.")
        names.add(name.casefold())
        redirects = file.get("redirect_urls", [])
        if not isinstance(redirects, list) or len(redirects) > 3:
            raise ValueError("Give at most three exact redirect URLs per file.")
        entry = {"filename": name, "url": _url(file["url"]), "redirect_urls": sorted(set(map(_url, redirects)))}
        if "sha256" in file:
            digest = file["sha256"]
            if not isinstance(digest, str) or not re.fullmatch("[0-9a-fA-F]{64}", digest):
                raise ValueError("An optional expected SHA-256 must be the complete 64 hex digits.")
            entry["sha256"] = digest.lower()
        result.append(entry)
    return sorted(result, key=lambda f: f["filename"])


def _id(number):
    if type(number) is not int or not 0 < number < 2**63:
        raise ValueError("Use the numeric file import request or grant ID from the room.")
    return number


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FileGrants:
    def __init__(self, state):
        self.path = state / "file-imports.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS grants (
                    id INTEGER PRIMARY KEY, project TEXT NOT NULL, collection TEXT NOT NULL,
                    files TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    mode TEXT, requested_at TEXT NOT NULL, decided_at TEXT, revoked_at TEXT);
                CREATE INDEX IF NOT EXISTS import_scope ON grants(project,collection,id);
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY, grant_id INTEGER NOT NULL REFERENCES grants(id),
                    filename TEXT NOT NULL, url TEXT NOT NULL, final_url TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
                    call_key TEXT NOT NULL UNIQUE, prepared_at TEXT NOT NULL, delivered_at TEXT);
                CREATE INDEX IF NOT EXISTS file_receipts ON transfers(grant_id,filename,id);
                CREATE TABLE IF NOT EXISTS transfer_retries (
                    call_key TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES transfers(id));
                """)

    @contextmanager
    def connect(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=60)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _entry(row):
        entry = dict(row)
        entry["files"] = json.loads(entry["files"])
        return entry

    def propose(self, project, collection, files, *, operator=False):
        """Record a proposal only. Issuing a grant requires a separate room decision."""
        project, collection, files = _label(project), _label(collection), _manifest(files)
        encoded = json.dumps(files, separators=(",", ":"))
        with self.connect(write=True) as db:
            rows = db.execute("""SELECT * FROM grants WHERE project=? AND collection=?
                                 ORDER BY (status='approved') DESC,id DESC""", (project, collection))
            for row in rows:
                entry = self._entry(row)
                if row["status"] == "approved" and all(f in entry["files"] for f in files):
                    return entry
                if row["files"] == encoded and (not operator or row["status"] == "pending"):
                    return entry
            if db.execute("SELECT count(*) FROM grants WHERE status='pending'").fetchone()[0] >= 16:
                raise ValueError("The room already has 16 pending requests. Resolve an existing request first.")
            number = db.execute("""INSERT INTO grants(project,collection,files,requested_at)
                                   VALUES(?,?,?,?)""", (project, collection, encoded, _now())).lastrowid
            return self._entry(db.execute("SELECT * FROM grants WHERE id=?", (number,)).fetchone())

    def decide(self, request_id, decision, *, room=None):
        choices = {"allow_once": ("approved", "once"), "allow_standing": ("approved", "standing"),
                   "deny": ("denied", None)}
        if not isinstance(decision, str) or decision not in choices:
            raise ValueError("Choose allow_once, allow_standing, or deny.")
        status, mode = choices[decision]
        with self.connect(write=True) as db:
            row = db.execute("SELECT status,mode,files FROM grants WHERE id=?", (_id(request_id),)).fetchone()
            if row is None:
                raise ValueError("Unknown file import request.")
            if row["status"] == "pending":
                db.execute("UPDATE grants SET status=?,mode=?,decided_at=? WHERE id=?",
                           (status, mode, _now(), request_id))
                if room is not None and status == "approved":
                    names = ", ".join(file["filename"] for file in json.loads(row["files"]))
                    scope = "one delivery of each listed file" if mode == "once" else "standing permission for repeat imports"
                    text = (f"The operator approved file import grant #{request_id} ({scope}). "
                            f"Files: {names}. Use import_file with grant_id {request_id} and the filename.")
                    room.record_import_approval(db, "file", request_id, mode, text)
            elif (row["status"], row["mode"]) != (status, mode):
                raise ValueError("This request already has a different decision. Refresh the room.")
            return {"id": request_id, "status": status, "mode": mode}

    def revoke(self, grant_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT status FROM grants WHERE id=?", (_id(grant_id),)).fetchone()
            if row is None or row["status"] not in ("approved", "revoked"):
                raise ValueError("Only an approved grant can be revoked.")
            if row["status"] == "approved":
                db.execute("UPDATE grants SET status='revoked',revoked_at=? WHERE id=?", (_now(), grant_id))
            return {"id": grant_id, "status": "revoked"}

    def overview(self):
        with self.connect() as db:
            rows = [self._entry(row) for row in db.execute(
                "SELECT * FROM grants WHERE status IN ('pending','approved') ORDER BY id DESC")]
            receipts = [self.receipt(row) for row in db.execute(
                "SELECT * FROM transfers WHERE delivered_at IS NOT NULL ORDER BY id DESC LIMIT 25")]
        return {"requests": [r for r in rows if r["status"] == "pending"],
                "grants": [r for r in rows if r["status"] == "approved"], "receipts": receipts}

    def status(self, request_id=None):
        with self.connect() as db:
            if request_id is not None:
                row = db.execute("SELECT * FROM grants WHERE id=?", (_id(request_id),)).fetchone()
                if row is None:
                    raise ValueError("Unknown file import request.")
                data = self._entry(row)
                data["receipts"] = [self.receipt(r) for r in db.execute(
                    "SELECT * FROM transfers WHERE grant_id=? AND delivered_at IS NOT NULL ORDER BY id DESC LIMIT 25",
                    (request_id,))]
                return data
            rows = db.execute("""SELECT * FROM grants ORDER BY id DESC LIMIT 40""")
            return {"recent_requests": [
                {"id": r["id"], "project": r["project"], "collection": r["collection"],
                 "status": r["status"], "mode": r["mode"], "filenames": [f["filename"] for f in json.loads(r["files"])]}
                for r in rows]}

    @staticmethod
    def scope(db, grant_id, filename):
        row = db.execute("SELECT * FROM grants WHERE id=?", (_id(grant_id),)).fetchone()
        if row is None or row["status"] not in ("approved", "fulfilled"):
            raise ValueError("This import needs an active grant from the private room. Check file_import_status.")
        spec = next((f for f in json.loads(row["files"]) if f["filename"] == filename), None)
        if spec is None:
            raise ValueError("This filename is outside the grant. Request approval for the new scope.")
        return {"grant_id": grant_id, "project": row["project"], "collection": row["collection"],
                "mode": row["mode"], "status": row["status"], "decided_at": row["decided_at"],
                "file": spec, "destination": "/work/imports/" + filename, "max_bytes": MAX_BYTES}

    def evidence(self, arguments):
        if set(arguments) != {"grant_id", "filename"}:
            raise ValueError("Supply the approved grant_id and filename.")
        with self.connect() as db:
            return self.scope(db, arguments["grant_id"], arguments["filename"])

    @staticmethod
    def receipt(row):
        return {key: row[key] for key in ("grant_id", "filename", "url", "final_url", "size_bytes",
                                           "sha256", "path", "delivered_at")}

    def prepared(self, scope, final_url, size, digest, call_key):
        with self.connect(write=True) as db:
            self.scope(db, scope["grant_id"], scope["file"]["filename"])
            return db.execute("""INSERT INTO transfers(grant_id,filename,url,final_url,size_bytes,sha256,path,
                                 call_key,prepared_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                              (scope["grant_id"], scope["file"]["filename"], scope["file"]["url"], final_url,
                               size, digest, scope["destination"], call_key, _now())).lastrowid

    def prior(self, db, grant_id, filename, mode, call_key):
        exact = db.execute("""SELECT * FROM transfers WHERE call_key=? OR
            id=(SELECT transfer_id FROM transfer_retries WHERE call_key=?)""", (call_key, call_key)).fetchone()
        if exact:
            if (exact["grant_id"], exact["filename"]) != (grant_id, filename):
                raise ValueError("This tool call ID already identifies a different import.")
            return exact
        prior = db.execute("""SELECT * FROM transfers WHERE grant_id=? AND filename=?
            AND (delivered_at IS NULL OR ?='once') ORDER BY id DESC LIMIT 1""",
                           (grant_id, filename, mode)).fetchone()
        if prior and mode == "standing":
            db.execute("INSERT INTO transfer_retries VALUES(?,?)", (call_key, prior["id"]))
        return prior

    def pending_objects(self):
        with self.connect() as db:
            return {r["sha256"] for r in db.execute("""SELECT DISTINCT t.sha256 FROM transfers t
                JOIN grants g ON g.id=t.grant_id WHERE g.status='approved' AND t.delivered_at IS NULL""")}

    def confirm(self, db, transfer_id):
        db.execute("UPDATE transfers SET delivered_at=? WHERE id=?", (_now(), transfer_id))
        row = db.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        grant_id = row["grant_id"]
        db.execute("""UPDATE grants SET status='fulfilled' WHERE id=? AND mode='once' AND
                     json_array_length(files)=(SELECT count(DISTINCT filename) FROM transfers
                     WHERE grant_id=? AND delivered_at IS NOT NULL)""", (grant_id, grant_id))
        return self.receipt(row)
