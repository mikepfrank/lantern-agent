"""Anonymous, bounded GitHub identity lookup and shallow Git pack transport."""

from contextlib import closing, contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from urllib.parse import quote

from .page_fetch import _PublicConnection, PageFetchError


MAX_PACK_BYTES = 24 * 1024 * 1024
MAX_ENTRIES = 8192
MAX_CHECKOUT_BYTES = 48 * 1024 * 1024  # Enforced in the offline receiver.
MAX_WIRE_BYTES = MAX_PACK_BYTES + 256 * 1024
DEADLINE_SECONDS = 75
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OWNER = r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?"
_REPO = r"[a-z0-9_][a-z0-9_.-]{0,99}"


class CloneFetchError(ValueError):
    """A diagnostic suitable for the private room."""


def normalize_repository(value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 180:
        raise CloneFetchError("Give a public GitHub owner/repository or its canonical HTTPS URL.")
    slug = value.lower().removeprefix("https://github.com/").removesuffix("/").removesuffix(".git")
    if (not re.fullmatch(_OWNER + "/" + _REPO, slug) or slug.endswith((".", ".git"))
            or slug in ("", ".", "..")):
        raise CloneFetchError("Give a public GitHub owner/repository or its canonical HTTPS URL.")
    return slug


def validate_branch(value):
    # Keep the branch exactly as GitHub spells it, including printable Unicode.
    # It is encoded as a URL path for lookup and never supplied to a shell.
    if (not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isprintable()
            or any(ch in " ~^:?*[\\" for ch in value)
            or value == "HEAD" or value.startswith("-") or value.endswith(".")
            or ".." in value or "@{" in value
            or any(not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise CloneFetchError("Use a valid Git branch name of at most 128 printable characters, or omit branch to use the repository default.")
    return value


def _identity(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise CloneFetchError("Invalid GitHub repository identity.")
    return value


@contextmanager
def _response(host, method, path, *, headers, body=None):
    headers = {"User-Agent": "Lantern-Research-Clone/1", "Connection": "close",
               "Accept-Encoding": "identity", **headers}
    with closing(_PublicConnection(host, port=443, timeout=8)) as connection:
        connection.request(method, path, body=body, headers=headers)
        with connection.getresponse() as response:
            if response.status != 200:
                if 300 <= response.status < 400:
                    raise CloneFetchError("GitHub redirected this repository. Request its current canonical name.")
                if response.status in (401, 404):
                    raise CloneFetchError("GitHub could not find this public repository, branch, or commit.")
                if response.status in (403, 429):
                    raise CloneFetchError("Anonymous GitHub access is limited or forbidden. Retry later.")
                raise CloneFetchError(f"GitHub returned HTTP {response.status}.")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise CloneFetchError("GitHub returned an unexpected transfer encoding.")
            yield response


def _api(path):
    limit = 128 * 1024
    with _response("api.github.com", "GET", path, headers={"Accept": "application/vnd.github+json"}) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise CloneFetchError("GitHub repository metadata exceeded the size limit.")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise CloneFetchError("GitHub returned invalid repository metadata.")
    return data


def _repository(repo):
    info = _api("/repos/" + repo)
    name = info.get("full_name")
    if info.get("private") is not False or not isinstance(name, str) or name.lower() != repo:
        raise CloneFetchError("The public repository no longer matches its canonical owner and name.")
    _identity(info.get("id"))
    return info


def _resolve(repo, branch):
    info = _repository(repo)
    branch = validate_branch(info.get("default_branch") if branch is None else branch)
    # Identity-based lookup keeps the selected branch and repository together across renames.
    ref = _api(f"/repositories/{info['id']}/git/ref/heads/" + quote(branch, safe="/"))
    obj = ref.get("object")
    if (ref.get("ref") != "refs/heads/" + branch or not isinstance(obj, dict)
            or obj.get("type") != "commit" or not _SHA.fullmatch(str(obj.get("sha", "")))):
        raise CloneFetchError("GitHub did not return an exact commit for this branch.")
    return {"repository_id": info["id"], "branch": branch, "commit": obj["sha"]}


def _line(text):
    raw = text.encode("ascii") + b"\n"
    return f"{len(raw) + 4:04x}".encode("ascii") + raw


def _pack_response(response, output, commit):
    # Protocol v2 fetch with 'done', depth=1 and no 'have' lines returns a
    # packfile, optionally preceded by a single shallow boundary. Pack data
    # travels on sideband 1. No other URLs, dependencies or commands are used.
    wire_bytes = 0

    def packet():
        nonlocal wire_bytes
        raw = response.read(4)
        if not re.fullmatch(rb"[0-9a-fA-F]{4}", raw):
            raise CloneFetchError("Incomplete Git pack framing.")
        n = int(raw, 16)
        wire_bytes += max(4, n)
        if wire_bytes > MAX_WIRE_BYTES or n > 65520 or n in (2, 3, 4):
            raise CloneFetchError("The Git pack exceeded the transfer limit or has invalid framing.")
        if n in (0, 1):
            return n
        data = response.read(n - 4)
        if len(data) != n - 4:
            raise CloneFetchError("The Git pack transfer was interrupted.")
        return data

    section = packet()
    if section == b"shallow-info\n":
        boundary = packet()
        if boundary != 1:
            if (not isinstance(boundary, bytes) or boundary.rstrip(b"\n") != b"shallow " + commit.encode()
                    or packet() != 1):
                raise CloneFetchError("The shallow boundary did not match the selected revision.")
        section = packet()
    if section != b"packfile\n":
        raise CloneFetchError("GitHub could not supply a shallow pack for this commit.")

    length = 0
    header = b""
    trailer = b""
    pack_hash = hashlib.sha1()  # Git's SHA-1 pack trailer, independently of our SHA-256 receipt.
    digest = hashlib.sha256()
    while True:
        chunk = packet()
        if chunk == 0:
            break
        if not isinstance(chunk, bytes) or chunk[0] != 1:
            raise CloneFetchError("GitHub did not complete the requested pack.")
        data = chunk[1:]
        length += len(data)
        if length > MAX_PACK_BYTES:
            raise CloneFetchError("The shallow pack exceeds 24 MiB.")
        header += data[:12 - len(header)]
        joined = trailer + data
        pack_hash.update(joined[:-20])
        trailer = joined[-20:]
        digest.update(data)
        output.write(data)
    if response.read(5) not in (b"", b"0002"):
        raise CloneFetchError("Unexpected data after the Git pack.")
    if (length < 32 or header[:4] != b"PACK" or trailer != pack_hash.digest()):
        raise CloneFetchError("The Git pack was incomplete or its checksum did not match.")
    version, count = struct.unpack(">II", header[4:])
    if version not in (2, 3) or not 2 <= count <= MAX_ENTRIES + 2:
        raise CloneFetchError("The Git pack format or object count exceeds the workspace limits.")
    return {"size_bytes": length, "sha256": digest.hexdigest()}


def _fetch(repo, commit, repository_id, output):
    current_id = _repository(repo)["id"]
    if repository_id is not None and current_id != repository_id:
        raise CloneFetchError("GitHub now assigns this name to a different repository. Request a new grant.")
    request = (_line("command=fetch") + b"0001" + _line("no-progress") + _line("ofs-delta")
               + _line("want " + commit) + _line("deepen 1") + _line("done") + b"0000")
    headers = {"Git-Protocol": "version=2", "Content-Type": "application/x-git-upload-pack-request",
               "Accept": "application/x-git-upload-pack-result"}
    with _response("github.com", "POST", "/" + repo + ".git/git-upload-pack", headers=headers, body=request) as r:
        if r.getheader("Content-Type", "").split(";")[0].strip() != headers["Accept"]:
            raise CloneFetchError("GitHub did not return a Git upload-pack response.")
        result = _pack_response(r, output, commit)
    if _repository(repo)["id"] != current_id:
        raise CloneFetchError("The repository identity changed during transfer. Request a new grant.")
    return result


def _child():
    try:
        args = json.load(sys.stdin)
        if args["op"] == "resolve":
            result = _resolve(args["repo"], args["branch"])
        else:
            result = _fetch(args["repo"], args["commit"], args["repository_id"], sys.stdout.buffer)
            sys.stdout.buffer.flush()
    except CloneFetchError as exc:
        result = {"error": str(exc)}
    except (PageFetchError, OSError, ValueError, http.client.HTTPException):
        result = {"error": "GitHub could not complete the public repository request."}
    print(json.dumps(result, ensure_ascii=True), file=sys.stderr)


def _run(args, output):
    # Bound DNS and trickling reads by a wall-clock deadline. The child receives
    # no GitHub token, Git configuration, proxy, cookie or workspace path.
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
    runner = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from lantern.clone_fetch import _child; _child()")
    try:
        done = subprocess.run([sys.executable, "-I", "-c", runner, str(Path(__file__).resolve().parent.parent)],
                              input=json.dumps(args).encode("ascii"), stdout=output, stderr=subprocess.PIPE,
                              env=env, timeout=DEADLINE_SECONDS, check=True)
        result = json.loads(done.stderr)
    except subprocess.TimeoutExpired:
        raise CloneFetchError("The public GitHub request exceeded its time limit.") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise CloneFetchError("The public GitHub request could not be completed.") from None
    if not isinstance(result, dict):
        raise CloneFetchError("Invalid GitHub lookup result.")
    if "error" in result:
        raise CloneFetchError(result["error"])
    return result


def resolve_branch(repo, branch=None):
    """Resolve an exact public repo identity and branch before a room decision."""
    return _run({"op": "resolve", "repo": normalize_repository(repo),
                 "branch": None if branch is None else validate_branch(branch)}, subprocess.DEVNULL)


def fetch_pack(repo, commit, destination, *, repository_id=None):
    """Write a selected shallow pack to the caller's private staging file."""
    repo = normalize_repository(repo)
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise CloneFetchError("A repository clone needs a full resolved commit.")
    if repository_id is not None:
        _identity(repository_id)
    with open(destination, "wb") as output:
        try:
            return _run({"op": "fetch", "repo": repo, "commit": commit, "repository_id": repository_id}, output)
        except BaseException:
            output.truncate(0)
            raise
