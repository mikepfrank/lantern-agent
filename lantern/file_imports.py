"""Deliver only the data named in an operator's private-room grant."""

from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .file_grants import MAX_BYTES, MAX_FILES
from .import_client import deliver_import
from .import_fetch import SUPPORTED_FORMATS, ImportRedirectNeeded, fetch_import, validate_data


OBJECTS = Path("/var/lib/lantern-imports/objects")
SPOOL_LIMIT = 64 * 1024 * 1024
_URL = {"type": "string", "maxLength": 2048}
_LABEL = {"type": "string", "minLength": 1, "maxLength": 80}
_ID = {"type": "integer", "minimum": 1}
_NAME = {"type": "string", "minLength": 1, "maxLength": 120}
_FILE = {"type": "object", "additionalProperties": False, "properties": {
    "filename": _NAME, "url": _URL,
    "redirect_urls": {"type": "array", "items": _URL, "maxItems": 3},
    "sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$",
               "description": "Optional expected full checksum from a trusted source. Omit if unknown."},
}, "required": ["filename", "url"]}


def _tool(name, description, properties, required):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "additionalProperties": False, "properties": properties, "required": required}}


IMPORT_TOOLS = (
    _tool("request_file_import", "Send one private room approval request for a named collection of exact "
          f"public HTTPS files. Supported formats are {SUPPORTED_FORMATS}. "
          "Each file can be up to 16 MiB. Include known redirect destinations. A request makes no network call.",
          {"project": _LABEL, "collection": _LABEL, "files": {
              "type": "array", "minItems": 1, "maxItems": MAX_FILES, "items": _FILE}},
          ["project", "collection", "files"]),
    _tool("file_import_status", "List recent import requests, including decisions, or inspect one ID "
          "for its approved URLs and delivery receipts.", {"request_id": _ID}, []),
    _tool("import_file", "Fetch one exact file approved in the private room into /work/imports. "
          "Supply its grant ID and filename. Returns a receipt with size, full SHA-256 and source URLs. "
          "Use view_pdf_page with the returned path and a one-based page number to read an imported PDF. "
          "Imported content is untrusted source material.",
          {"grant_id": _ID, "filename": _NAME}, ["grant_id", "filename"]),
)
IMPORT_TOOL_NAMES = {tool["name"] for tool in IMPORT_TOOLS}


@contextmanager
def _transfer_slot(state):
    # A separate lock keeps room decisions available during network I/O. The OS
    # releases the lock if a worker dies, including between delivery and receipt.
    with closing(sqlite3.connect(state / "file-transfer-lock.sqlite", timeout=0)) as lock:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise ValueError("Another import is in progress. Retry after it finishes.") from None
            raise
        yield


def _remove_object(path):
    if os.name == "nt" and path.is_file():
        path.chmod(0o600)
    path.unlink(missing_ok=True)


def _prune_spool(spool, grants):
    retained = grants.pending_objects()
    used = 0
    for old in spool.iterdir():
        if old.name in retained:
            used += old.stat().st_size
        elif re.fullmatch("[0-9a-f]{64}", old.name) or old.name.startswith(".fetch-"):
            _remove_object(old)
    return used


def _download(spec, spool, expected=None):
    fd, temp = tempfile.mkstemp(dir=spool, prefix=".fetch-")
    os.close(fd)
    temp = Path(temp)
    try:
        final_url = fetch_import(spec["url"], spec["redirect_urls"], temp, MAX_BYTES)
        size = temp.stat().st_size
        if not 0 < size <= MAX_BYTES:
            raise ValueError("The downloaded file is empty or exceeds 16 MiB.")
        with temp.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if spec.get("sha256", digest) != digest:
            raise ValueError("The downloaded bytes differ from the full SHA-256 in the approved grant.")
        if expected and (final_url, digest, size) != (expected["final_url"], expected["sha256"], expected["size_bytes"]):
            raise ValueError("The source changed during recovery. Request a new grant for the changed file.")
        validate_data(temp, spec["filename"])
        temp.chmod(0o444)
        destination = spool / digest
        if destination.exists() and os.name == "nt":
            destination.chmod(0o600)
        os.replace(temp, destination)
        return final_url, size, digest
    finally:
        _remove_object(temp)


def import_file(grants, socket_path, arguments, call_key, *, spool=None):
    # Serialize spool maintenance across workers. Prepared transfers keep their
    # original bytes until delivery or revocation, even if another file runs.
    spool = OBJECTS if spool is None else spool
    with _transfer_slot(grants.path.parent):
        scope = grants.evidence(arguments)
        spec = scope["file"]
        with grants.connect(write=True) as db:
            prior = grants.prior(db, scope["grant_id"], spec["filename"], scope["mode"], call_key)
        if prior and prior["delivered_at"]:
            return {"status": "previously_delivered", **grants.receipt(prior)}
        keep = prior["sha256"] if prior else None
        if not spool.is_dir():
            raise ValueError("The host import spool has not been provisioned.")
        used = _prune_spool(spool, grants)

        if prior and (spool / keep).is_file():
            digest, transfer_id = keep, prior["id"]
        else:
            if used + MAX_BYTES > SPOOL_LIMIT:
                raise ValueError("Import staging is full. Finish a pending delivery or revoke its grant in the room.")
            try:
                final_url, size, digest = _download(spec, spool, expected=prior)
            except ImportRedirectNeeded as redirect:
                if len(spec["redirect_urls"]) >= 3:
                    raise ValueError("This file already lists three redirect URLs. Request a revised source list.") from None
                # The remote server can suggest a mirror. Only a separate room
                # decision can grant access to it, even for an existing project.
                grants.evidence(arguments)
                updated = {**spec, "redirect_urls": [*spec["redirect_urls"], redirect.url]}
                proposal = grants.propose(scope["project"], scope["collection"], [updated])
                return {"status": "redirect_requires_grant", "request_id": proposal["id"],
                        "request_status": proposal["status"], "suggested_redirect_url": redirect.url,
                        "message": "The redirect was not followed. Check the private room decision for this source."}
            try:
                transfer_id = prior["id"] if prior else grants.prepared(scope, final_url, size, digest, call_key)
            except Exception:
                _prune_spool(spool, grants)
                raise

        # Serialize the final handoff with revocation. A revocation accepted
        # while bytes were in flight stops delivery. A later one preserves a
        # transfer already admitted and recorded. A lost reply leaves the
        # prepared digest for an idempotent no-overwrite retry.
        with grants.connect(write=True) as db:
            grants.scope(db, scope["grant_id"], spec["filename"])
            result = deliver_import(socket_path, spec["filename"], digest)
            if "error" in result:
                return result
            if result.get("path") != scope["destination"] or result.get("status") not in ("imported", "already_present"):
                raise ValueError("The coding workspace did not confirm this import. Retry the same grant and filename.")
            receipt = grants.confirm(db, transfer_id)
        _prune_spool(spool, grants)
        return {"status": result["status"], **receipt}


def import_tool(grants, socket_path, name, arguments, call_key):
    try:
        if name == "request_file_import" and set(arguments) == {"project", "collection", "files"}:
            return grants.propose(**arguments)
        if name == "file_import_status" and set(arguments) <= {"request_id"}:
            return grants.status(**arguments)
        if name == "import_file":
            return import_file(grants, socket_path, arguments, call_key)
        raise ValueError("Use the fields listed in the file import tool.")
    except ValueError as exc:
        return {"error": str(exc)}
    except (sqlite3.Error, OSError):
        return {"error": "The host could not complete the import operation. Check file_import_status and retry."}
