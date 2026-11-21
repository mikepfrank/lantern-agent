"""Private, time-limited sampling of live Opus referrals for an independent reviewer."""

from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import Event

from .budget import UNITS_PER_DOLLAR
from .chat_worker import worker_lock
from .shadow_vendor import MAX_CALLS as CASE_LIMIT, PILOT_CAP


WINDOW = timedelta(days=7)
PILOT_NAME = "sonnet-live-v1"


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _decisions(result):
    if result.get("status") != "ok":
        return None
    verdict = result["verdict"]
    concerns = [verdict["concern"]] if "concern" in verdict else verdict.get("concerns", [])
    return (verdict.get("decision"), sorted(
        (c["id"], c["decision"], sorted(c["tools"])) for c in concerns))


def _asks_operator(result):
    decisions = _decisions(result)
    return bool(decisions and (decisions[0] == "ask_operator" or
                               any(label == "ask_operator" for _, label, _ in decisions[1])))


class ShadowPilot:
    """Activation is explicit and idempotent, including after the pilot ends."""

    def __init__(self, state: Path, *, clock=None):
        self.directory = state / "operator-trials" / PILOT_NAME
        self.path = self.directory / "pilot.sqlite"
        self.ledger = state / "review-costs.sqlite"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _db(self, write=False, timeout=5):
        mode = "rw" if write else "ro"
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=" + mode,
                                     uri=True, timeout=timeout)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def activate(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        now = self.clock()
        with self._db(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS campaign (
                id INTEGER PRIMARY KEY CHECK(id=1), started TEXT NOT NULL, deadline TEXT NOT NULL,
                closed TEXT, reason TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS cases (
                opus_id TEXT PRIMARY KEY, shadow_id TEXT UNIQUE NOT NULL, at TEXT NOT NULL,
                thread_id TEXT NOT NULL, batch_id INTEGER, tool TEXT, kind TEXT NOT NULL,
                sample TEXT, opus TEXT NOT NULL, state TEXT NOT NULL, exclusion TEXT, sonnet TEXT)""")
            db.execute("INSERT OR IGNORE INTO campaign(id,started,deadline) VALUES(1,?,?)",
                       (now.isoformat(), (now + WINDOW).isoformat()))
        return self.status()

    def _billing(self, ids):
        ids = list(ids)
        if not ids or not self.ledger.is_file():
            return {}
        with closing(sqlite3.connect(self.ledger.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(f"SELECT id,cost,reserved,ms,status FROM calls WHERE id IN ({placeholders})", ids)
            return {r["id"]: {"cost_usd": str(Decimal(r["cost"]) / UNITS_PER_DOLLAR) if r["cost"] is not None else None,
                              "held_usd": str(Decimal(r["reserved"]) / UNITS_PER_DOLLAR) if r["cost"] is None else "0",
                              "ms": r["ms"], "status": r["status"]} for r in rows}

    def record(self, *, key, thread_id, batch_id, tool, kind, sample, opus, exclusion=None):
        """Called after a live ruling. No provider calls, shared ledger, or work on an inactive host."""
        if not self.path.is_file():
            return False
        now = self.clock().isoformat()
        with self._db(write=True, timeout=0.1) as db:
            campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            if not campaign or campaign["closed"] or now >= campaign["deadline"]:
                return False
            if db.execute("SELECT count(*) FROM cases WHERE exclusion IS NULL").fetchone()[0] >= CASE_LIMIT:
                return False
            shadow_id = hashlib.sha256((PILOT_NAME + "\0" + key).encode()).hexdigest()[:32]
            if exclusion:
                opus = {"status": opus["status"], "error": opus.get("error")}
            accepted = db.execute("""INSERT INTO cases(opus_id,shadow_id,at,thread_id,batch_id,tool,kind,sample,
                    opus,state,exclusion) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(opus_id) DO NOTHING""",
                (key, shadow_id, now, thread_id, batch_id, tool, kind, _json(sample) if not exclusion else None,
                 _json(opus), "excluded" if exclusion else "queued", exclusion)).rowcount
            return bool(accepted)

    @staticmethod
    def _close(db, now, reason):
        db.execute("UPDATE campaign SET closed=?, reason=? WHERE id=1 AND closed IS NULL", (now, reason))
        db.execute("UPDATE cases SET state='skipped',sonnet=? WHERE state='queued'", (_json({"status": reason}),))

    def stop(self):
        if self.path.is_file():
            with self._db(write=True) as db:
                self._close(db, self.clock().isoformat(), "operator_stop")
        return self.status()

    def _recover(self):
        # Called only while holding the exclusive shadow consumer lock. A durable reservation
        # means dispatch might have happened; a missing comparison is safer than a second call.
        with self._db(write=True) as db:
            pending = [row[0] for row in db.execute("SELECT shadow_id FROM cases WHERE state='working'")]
            receipts = self._billing(pending)
            for request_id in pending:
                if request_id in receipts or not self.ledger.is_file():
                    db.execute("UPDATE cases SET state='finished',sonnet=? WHERE shadow_id=?",
                               (_json({"status": "interrupted_unknown_result"}), request_id))
                else:
                    db.execute("UPDATE cases SET state='queued' WHERE shadow_id=?", (request_id,))
            campaign = db.execute("SELECT closed,reason FROM campaign WHERE id=1").fetchone()
            if campaign["closed"]:
                self._close(db, campaign["closed"], campaign["reason"])

    def drain_one(self, reviewer):
        """Return 'processed', 'idle', or 'closed'. Only the dedicated consumer calls this."""
        now = self.clock().isoformat()
        with self._db(write=True) as db:
            campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            if campaign["closed"]:
                return "closed"
            if now >= campaign["deadline"]:
                self._close(db, now, "expired")
                return "closed"
            case = db.execute("""SELECT shadow_id AS request_id,thread_id,batch_id,tool,kind,sample
                FROM cases WHERE state='queued' ORDER BY rowid LIMIT 1""").fetchone()
            if case is None:
                count, = db.execute("SELECT count(*) FROM cases WHERE exclusion IS NULL").fetchone()
                if count >= CASE_LIMIT:
                    self._close(db, now, "sample_limit")
                    return "closed"
                return "idle"
            request = dict(case)
            db.execute("UPDATE cases SET state='working' WHERE shadow_id=?", (request["request_id"],))
        request["sample"] = json.loads(request["sample"])
        deadline = datetime.fromisoformat(campaign["deadline"])
        try:
            result = reviewer(request, deadline)
            if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                raise ValueError("Missing shadow outcome")
            encoded = _json(result)
        except Exception as exc:
            result = {"status": "shadow_error", "error": type(exc).__name__}
            encoded = _json(result)
        with self._db(write=True) as db:
            db.execute("UPDATE cases SET state='finished',sonnet=? WHERE shadow_id=?",
                       (encoded, request["request_id"]))
            if result["status"] in ("pilot_cap", "sample_limit", "expired"):
                self._close(db, self.clock().isoformat(), result["status"])
                return "closed"
        return "processed"

    def results(self, *, include_evidence=False):
        if not self.path.is_file():
            return []
        columns = "sample, " if include_evidence else ""
        with self._db() as db:
            rows = [dict(row) for row in db.execute(f"""SELECT {columns}opus_id,shadow_id,at,thread_id,batch_id,
                tool,kind,state,exclusion,opus,sonnet FROM cases ORDER BY rowid""")]
        costs = self._billing(r[id_field] for r in rows for id_field in ("opus_id", "shadow_id"))
        for row in rows:
            row["opus"] = json.loads(row["opus"])
            row["sonnet"] = json.loads(row["sonnet"]) if row["sonnet"] else {}
            opus, sonnet = _decisions(row["opus"]), _decisions(row["sonnet"])
            row["comparison"] = ("agree" if opus == sonnet else "disagree") if opus and sonnet else None
            row["opus_usage"] = costs.get(row["opus_id"])
            row["sonnet_usage"] = costs.get(row["shadow_id"])
            if include_evidence:
                row["sample"] = json.loads(row["sample"]) if row["sample"] else None
        return rows

    def status(self):
        if not self.path.is_file():
            return {"state": "not_started", "enrolled": 0, "case_limit": CASE_LIMIT, "limit_usd": str(PILOT_CAP)}
        with self._db() as db:
            campaign = dict(db.execute("SELECT started,deadline,closed,reason FROM campaign WHERE id=1").fetchone())
        rows = self.results()
        counts = Counter(r["state"] for r in rows)
        enrolled = len(rows) - counts["excluded"]
        if campaign["closed"]:
            state = "closed"
        elif self.clock().isoformat() >= campaign["deadline"]:
            state = "expired"
        else:
            state = "draining" if enrolled >= CASE_LIMIT else "collecting"
        usage = [r["sonnet_usage"] for r in rows if r["sonnet_usage"]]
        return {**campaign, "state": state, "case_limit": CASE_LIMIT, "limit_usd": str(PILOT_CAP), "enrolled": enrolled,
                "case_states": dict(counts), "paired": dict(Counter(r["comparison"] for r in rows if r["comparison"])),
                "referral_kinds": dict(Counter(r["kind"] for r in rows if not r["exclusion"])),
                "opus_outcomes": dict(Counter(r["opus"]["status"] for r in rows if not r["exclusion"])),
                "operator_questions": {m: sum(_asks_operator(r[m]) for r in rows if not r["exclusion"])
                                       for m in ("opus", "sonnet")},
                "sonnet_outcomes": dict(Counter(r["sonnet"]["status"] for r in rows if r["sonnet"])),
                "excluded": dict(Counter(r["exclusion"] for r in rows if r["exclusion"])),
                "cost_usd": str(sum((Decimal(r["cost_usd"] or "0") for r in usage), Decimal(0))),
                "held_usd": str(sum((Decimal(r["held_usd"]) for r in usage), Decimal(0)))}


def run_shadow(state, reviewer, *, stop=None, poll_seconds=5):
    """Run in the separate host service. A service stop leaves the queue resumable."""
    pilot = ShadowPilot(state)
    if not pilot.path.is_file():
        return
    stop = stop or Event()
    with worker_lock(pilot.directory):
        pilot._recover()
        while not stop.is_set():
            outcome = pilot.drain_one(reviewer)
            if outcome == "closed":
                return
            if outcome == "idle":
                stop.wait(poll_seconds)
