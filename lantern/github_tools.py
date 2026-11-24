"""Worker tools for the host's fixed-repository GitHub broker."""

import hashlib
import json
import re
import socket
import time

from .digest_prefix import DIGEST_PREFIX_PATTERN, MIN_DIGEST_PREFIX, normalize_digest_prefix


REPOSITORY = "mikepfrank/lantern-agent"
OPEN_DRAFT = "github_open_draft"
PATCH_LIMIT = 12 * 1024
FRAME_LIMIT = 128 * 1024
COMMIT_PATTERN = "[0-9a-f]{40}"
PROPOSAL_PATTERN = "[0-9a-f]{64}"


class ProposalLookupError(ValueError):
    """The agent can correct its frozen-proposal selector and try again."""


def _tool(name, description, properties):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "properties": properties, "required": list(properties)}}


GITHUB_TOOLS = (
    _tool("github_sync",
          "Fetch the designated public repository's main branch into /work/lantern as upstream/main. "
          "A clean branch can fast-forward. Local commits and edits are preserved. Inspect the result "
          "and use coding_run for local integration when branches have diverged.", {}),
    _tool("github_preview",
          "Prepare a private preview of a small committed text patch against upstream/main. "
          "Sync first, then use the full commit ID from git rev-parse HEAD. Read the returned "
          "public destination, file paths, exact patch, title and body before requesting publication.",
          {"commit": {"type": "string", "pattern": "^" + COMMIT_PATTERN + "$"}}),
    _tool(OPEN_DRAFT,
          "Publish the exact frozen preview as a public draft pull request for Mike's review in "
          + REPOSITORY + f". Supply proposal_id_prefix from github_preview, or any unique "
          f"{MIN_DIGEST_PREFIX}–64 hex digits starting the proposal ID. The tool can create a proposal branch and draft PR. "
          "Mike controls merge and deployment. Keep private material out of the entire public patch.",
          {"proposal_id": {"type": "string", "pattern": DIGEST_PREFIX_PATTERN}}),
)


def _hex(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _call(socket_path, op, args):
    frame = json.dumps({"op": op, **args}, separators=(",", ":")).encode("ascii") + b"\n"
    deadline = time.monotonic() + (150 if op == "open_draft" else 115)
    if not hasattr(socket, "AF_UNIX"):
        return {"error": "The GitHub broker requires a local Unix socket."}
    sent = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(deadline - time.monotonic())
            peer.connect(str(socket_path))
            sent = True
            peer.sendall(frame)
            reply = bytearray()
            while len(reply) < FRAME_LIMIT:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                peer.settimeout(remaining)
                block = peer.recv(min(8192, FRAME_LIMIT - len(reply)))
                if not block:
                    break
                reply.extend(block)
                if b"\n" in block:
                    if reply.count(b"\n") == 1 and reply.endswith(b"\n"):
                        result = json.loads(reply.decode("utf-8"))
                        if isinstance(result, dict) and result:
                            return result
                    break
    except (OSError, ValueError, RecursionError):
        pass
    if sent and op == "open_draft":
        return {"error": "GitHub publication status is unknown. Use the same proposal ID to check or retry."}
    if sent and op == "sync":
        return {"error": "Sync status is unknown. Inspect the checkout before continuing."}
    return {"error": "The GitHub broker is unavailable or returned an unreadable reply."}


def _frozen(result, *, prefix=None, commit=None):
    """Accept only a complete public preview with the content-addressed ID reported to Lantern."""
    document = dict(result)
    returned_id = document.pop("proposal_id", None)
    if (set(document) != {"repository", "base_branch", "base", "source_commit", "title", "body", "paths", "patch"}
            or not _hex(returned_id, PROPOSAL_PATTERN)
            or (prefix is not None and not returned_id.startswith(prefix))
            or (commit is not None and document["source_commit"] != commit)
            or document["repository"] != REPOSITORY or document["base_branch"] != "main"
            or not _hex(document["source_commit"], COMMIT_PATTERN) or not _hex(document["base"], COMMIT_PATTERN)
            or not isinstance(document["title"], str) or not 1 <= len(document["title"]) <= 120
            or not isinstance(document["body"], str) or len(document["body"]) > 512
            or not isinstance(document["paths"], list) or not 1 <= len(document["paths"]) <= 16
            or not all(isinstance(p, str) and p.isascii() and 1 <= len(p) <= 240 for p in document["paths"])
            or not isinstance(document["patch"], str) or not document["patch"]):
        raise ValueError("Incomplete GitHub proposal evidence")
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(document["patch"].encode("utf-8")) > PATCH_LIMIT or hashlib.sha256(encoded).hexdigest() != returned_id:
        raise ValueError("Frozen GitHub proposal does not match its ID or exceeds the patch limit")
    return result


def draft_evidence(socket_path, args):
    if set(args) != {"proposal_id"}:
        raise ProposalLookupError("Supply the proposal ID from github_preview.")
    try:
        prefix = normalize_digest_prefix(args["proposal_id"])
    except ValueError as exc:
        raise ProposalLookupError(str(exc)) from exc
    result = _call(socket_path, "evidence", {"proposal_id": prefix})
    if result.get("lookup_error") == "ambiguous":
        raise ProposalLookupError("That prefix matches multiple frozen proposals. Supply more digits from github_preview.")
    if result.get("lookup_error") == "missing":
        raise ProposalLookupError("No frozen proposal matches that ID. Use the proposal_id_prefix from github_preview.")
    return _frozen(result, prefix=prefix)


def github_tool(socket_path, name, args):
    if name == "github_sync" and args == {}:
        return _call(socket_path, "sync", {})
    if name == "github_preview" and set(args) == {"commit"} and _hex(args["commit"], COMMIT_PATTERN):
        result = _call(socket_path, "preview", args)
        if set(result) == {"error"}:
            return result
        try:
            preview = _frozen(result, commit=args["commit"])
            return {**preview, "proposal_id_prefix": preview["proposal_id"][:MIN_DIGEST_PREFIX]}
        except ValueError:
            return {"error": "The broker did not return a complete, verified preview. Prepare a fresh preview."}
    if name == OPEN_DRAFT and set(args) == {"proposal_id"} and _hex(args["proposal_id"], PROPOSAL_PATTERN):
        return _call(socket_path, "open_draft", args)
    raise ValueError("Unexpected GitHub tool arguments")
