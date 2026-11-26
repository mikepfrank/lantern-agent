"""Supervised repository proposals and delivery of an approved Git revision."""

import os
from pathlib import Path
import sqlite3
import tempfile

from .clone_client import deliver_clone
from .clone_fetch import MAX_CHECKOUT_BYTES, MAX_PACK_BYTES, fetch_pack
from .clone_grants import _commit, _now, checkout_path
from .file_imports import SPOOL_LIMIT, _prune_spool, _remove_object, _tool, _transfer_slot


PACKS = Path("/var/lib/lantern-imports/clone-packs")
_ID = {"type": "integer", "minimum": 1}
CLONE_TOOLS = (
    _tool("request_repository_clone",
          "Choose a public GitHub repository you want to study, even if Mike has never mentioned it. "
          "Submit its owner/repo or HTTPS URL and your reason for the operator to review privately. "
          "The host looks up only public repository and branch metadata before approval. "
          "Omit branch or pass an empty string to use GitHub's current default branch.",
          {"repository": {"type": "string", "maxLength": 180},
           "purpose": {"type": "string", "minLength": 1, "maxLength": 400},
           "branch": {"type": "string", "maxLength": 128,
                      "description": "Optional exact branch name. Omitted or empty uses the repository default."}},
          ["repository", "purpose"]),
    _tool("repository_clone_status", "List recent requests or look up one decision and its checkout paths.",
          {"request_id": _ID}, []),
    _tool("clone_repository", "Bring an approved shallow Git checkout into your offline workspace. "
          "Use the numeric grant ID. With standing permission, repeat to get updates to the same branch. "
          f"The download limit is {MAX_PACK_BYTES // (1024 * 1024)} MiB and the checkout limit is "
          f"{MAX_CHECKOUT_BYTES // (1024 * 1024)} MiB. "
          "Each new revision gets its own directory so earlier edits survive.",
          {"grant_id": _ID}, ["grant_id"]),
)
CLONE_TOOL_NAMES = {t["name"] for t in CLONE_TOOLS}


def _stage(scope, tip, spool, used):
    if used + MAX_PACK_BYTES > SPOOL_LIMIT:
        raise ValueError("Clone staging is full. Complete an interrupted clone or revoke its grant.")
    fd, name = tempfile.mkstemp(dir=spool, prefix=".fetch-")
    os.close(fd)
    temp = Path(name)
    try:
        pack = fetch_pack(scope["repository"], tip, temp, repository_id=scope["repository_id"])
        target = spool / pack["sha256"]
        temp.chmod(0o444)
        if os.name == "nt" and target.exists():
            target.chmod(0o600)
        os.replace(temp, target)
        return pack
    finally:
        _remove_object(temp)


def _deliver(grants, socket_path, grant_id, tip, call_key):
    """Called only with the host-resolved target from supervision."""
    tip = _commit(tip)
    with _transfer_slot(grants.path.parent):
        with grants.connect(write=True) as db:
            scope = grants.scope(db, grant_id)
            if scope["mode"] == "once" and scope["commit"] != tip:
                raise ValueError("The reviewed commit differs from the one-time approval.")
            prior = grants.prior(db, scope, call_key)
            if not prior and scope["mode"] == "standing":
                last = db.execute("""SELECT * FROM deliveries WHERE grant_id=? AND status='delivered'
                    ORDER BY id DESC LIMIT 1""", (grant_id,)).fetchone()
                if last and last["tip"] == tip:
                    db.execute("INSERT INTO calls VALUES(?,?)", (call_key, last["id"]))
                    return {"status": "previously_delivered", **grants.receipt(last, scope)}
            if prior:
                if prior["tip"] != tip:
                    raise ValueError("Another checkout changed the pending revision. Retry with the grant ID.")
                if prior["status"] == "delivered":
                    return {"status": "previously_delivered", **grants.receipt(prior, scope)}
                if prior["status"] == "failed":
                    raise ValueError("This attempt failed before checkout. Start a fresh tool call.")
        if not PACKS.is_dir():
            raise ValueError("The host has not provisioned repository cloning.")
        used = _prune_spool(PACKS, grants)
        cached = prior and (PACKS / prior["sha256"]).is_file()
        pack = dict(prior) if cached else _stage(scope, tip, PACKS, used)
        try:
            with grants.connect(write=True) as db:
                grants.scope(db, grant_id)
                if prior:
                    delivery = prior["id"]
                    db.execute("UPDATE deliveries SET sha256=?,size_bytes=? WHERE id=?",
                               (pack["sha256"], pack["size_bytes"], delivery))
                else:
                    delivery = db.execute("""INSERT INTO deliveries(grant_id,tip,previous_commit,size_bytes,sha256)
                        VALUES(?,?,(SELECT tip FROM deliveries WHERE grant_id=? AND status='delivered'
                                    ORDER BY id DESC LIMIT 1),?,?)""",
                                          (grant_id, tip, grant_id, pack["size_bytes"], pack["sha256"])).lastrowid
                db.execute("INSERT OR IGNORE INTO calls VALUES(?,?)", (call_key, delivery))
            # A committed 'prepared' row survives a lost broker reply. Holding
            # this final transaction orders delivery with signed-in revocation.
            with grants.connect(write=True) as db:
                grants.scope(db, grant_id)
                result = deliver_clone(socket_path, scope["repository"], tip, pack["sha256"])
                if "error" in result:
                    if result.get("definitive") is True:
                        db.execute("UPDATE deliveries SET status='failed' WHERE id=?", (delivery,))
                    return {"error": result["error"]}
                if (result.get("status") not in ("cloned", "already_present")
                        or result.get("path") != checkout_path(scope["repository"], tip)):
                    raise ValueError("The coding workspace did not confirm the checkout. Retry this grant.")
                db.execute("UPDATE deliveries SET status='delivered',delivered_at=? WHERE id=?", (_now(), delivery))
                if scope["mode"] == "once":
                    db.execute("UPDATE grants SET status='fulfilled' WHERE id=?", (grant_id,))
                row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery,)).fetchone()
                return {"status": result["status"], **grants.receipt(row, scope)}
        finally:
            _prune_spool(PACKS, grants)


def clone_tool(grants, socket_path, name, arguments, call_key):
    try:
        if name == "request_repository_clone" and set(arguments) in (
                {"repository", "purpose"}, {"repository", "purpose", "branch"}):
            return grants.propose(**arguments)
        if name == "repository_clone_status" and set(arguments) <= {"request_id"}:
            return grants.status(**arguments)
        if name == "clone_repository" and set(arguments) == {"grant_id", "target_commit"}:
            return _deliver(grants, socket_path, arguments["grant_id"], arguments["target_commit"], call_key)
        raise ValueError("Use the fields listed on the repository tool.")
    except ValueError as exc:
        return {"error": str(exc)}
    except (OSError, sqlite3.Error):
        return {"error": "The host could not complete this repository operation. Check repository_clone_status."}
