"""Host-mediated sync and draft proposals for the public Lantern repository."""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlencode


REPO = "mikepfrank/lantern-agent"
GIT_URL = f"https://github.com/{REPO}.git"
BASE_REF = "refs/heads/main"
PATCH_LIMIT = 12 * 1024
MAX_BLOB = 512 * 1024
MAX_TREE_PAYLOAD = 2 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
PROPOSAL_ID = re.compile(r"[0-9a-f]{64}")
SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
PUBLIC_ROOTS = {"lantern", "tests", "deploy", "docs"}
PRIVATE_NAME = re.compile(r"(?:^|[-_.])(heritage|lmstudio|transcript|credentials?|secrets?|private|"
                          r"chatlog|roomlog|id_rsa|id_ed25519)(?:[-_.]|$)", re.I)
SECRET = re.compile(r"(?:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
                    r"(?:github_pat_|gh[pousr]_|sk-(?:or-|ant-)?)[A-Za-z0-9_-]{18,}|"
                    r"(?:AKIA|ASIA)[A-Z0-9]{16}|https?://[^\s/@]+:[^\s/@]+@)", re.I)
UNSAFE_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


class ProposalError(ValueError):
    """A short operator/model-visible diagnostic without remote bodies or credentials."""


class ApiError(ProposalError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub responded with HTTP {status}. Check the repository credential and retry.")


def _api(token, method, endpoint, payload=None):
    # A fixed origin, direct TLS and no redirect handling keep the credential on GitHub's API.
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "lantern-draft-proposals"}
    conn = http.client.HTTPSConnection("api.github.com", timeout=12)
    try:
        conn.request(method, f"/repos/{REPO}/{endpoint}", body=data, headers=headers)
        resp = conn.getresponse()
        if not 200 <= resp.status < 300:
            raise ApiError(resp.status)
        raw = resp.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ProposalError("GitHub response exceeded the proposal limit.")
        return json.loads(raw)
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise ProposalError("GitHub's response was unavailable. Retry the same proposal ID.") from exc
    finally:
        conn.close()


def _git(args, *, repo=None, index=None, data=None):
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "TEMP", "TMP") if k in os.environ}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1", LC_ALL="C")
    if index:
        env["GIT_INDEX_FILE"] = str(index)
    argv = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "credential.helper=",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false",
            "-c", "core.autocrlf=false", "-c", "gc.auto=0"]
    if repo:
        argv += ["--git-dir", str(repo)]
    try:
        result = subprocess.run(argv + list(args), input=data if data is not None else b"",
                                capture_output=True, timeout=25, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProposalError(f"Trusted Git operation {args[0]} was unavailable.") from exc
    if result.returncode:
        raise ProposalError(f"Trusted Git operation {args[0]} failed. Sync main and check the patch.")
    return result.stdout


def _path_ok(path):
    parts = path.split("/")
    return (len(path) <= 240 and bool(SAFE_PATH.fullmatch(path))
            and (parts[0] in PUBLIC_ROOTS or path in ("README.md", "HANDOFF.md"))
            and not any(PRIVATE_NAME.search(p) or p.lower() in {"notes", "logs", "workspace", "heritage"}
                        for p in parts)
            and Path(path).suffix.lower() not in {".pem", ".key", ".db", ".sqlite", ".sqlite3", ".log",
                                                   ".pdf", ".bundle", ".patch", ".lock", ".bak"})


def proposal_id(evidence):
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


class GitHubFlow:
    """The caller serializes Git operations and owns this state outside Lantern's writable volume."""

    def __init__(self, state_dir, workspace, *, token=None):
        self.state = Path(state_dir).resolve()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.mirror = self.state / "public-main.git"
        self.workspace = workspace
        self.token = token

    def _fetch_main(self):
        if not self.mirror.is_dir():
            _git(["init", "--bare", "--template=", str(self.mirror)])
        _git(["fetch", "--no-tags", GIT_URL, f"+{BASE_REF}:{BASE_REF}"], repo=self.mirror)

    def sync(self):
        self._fetch_main()
        with TemporaryDirectory(dir=self.state) as folder:
            bundle = Path(folder) / "upstream.bundle"
            _git(["bundle", "create", str(bundle), BASE_REF], repo=self.mirror)
            bundle.chmod(0o644)
            # Only this public file is mounted for the duration of the offline import.
            return self.workspace({"op": "sync"}, bundle)

    @contextmanager
    def _staged(self, base, patch):
        if len(patch) > PATCH_LIMIT or b"GIT binary patch" in patch or b"Binary files " in patch:
            raise ProposalError("Submit a text-only patch of at most 12 KiB.")
        try:
            patch_text = patch.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProposalError("Submit a UTF-8 text patch.") from exc
        if UNSAFE_TEXT.search(patch_text) or SECRET.search(patch_text):
            raise ProposalError("The patch contains hidden control text or a possible credential.")
        if not self.mirror.is_dir():
            raise ProposalError("Run github_sync before preparing a draft.")
        _git(["merge-base", "--is-ancestor", base, BASE_REF], repo=self.mirror)
        with TemporaryDirectory(dir=self.state) as folder:
            index = Path(folder) / "index"
            def git(*args, data=None):
                return _git(args, repo=self.mirror, index=index, data=data)
            git("read-tree", base)
            git("apply", "--cached", "--whitespace=nowarn", "-", data=patch)
            raw = git("diff", "--cached", "--raw", "--no-abbrev", "--no-renames", "-z", base, "--")
            parts = raw.rstrip(b"\0").split(b"\0")
            if len(parts) % 2 or not 2 <= len(parts) <= 32:
                raise ProposalError("A proposal must change 1 to 16 regular text files.")
            entries = []
            total = 0
            for header, rawpath in zip(parts[::2], parts[1::2]):
                try:
                    path = rawpath.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ProposalError("Proposal filenames must be printable ASCII.") from exc
                match = re.fullmatch(rb":(000000|100644|100755) (000000|100644|100755) "
                                     rb"[0-9a-f]{40} ([0-9a-f]{40}) ([AMD])", header)
                if not match or not _path_ok(path):
                    raise ProposalError("A proposal path or file type is outside the public source, tests or documentation.")
                _, mode, blob, change = (x.decode("ascii") for x in match.groups())
                entry = {"path": path, "type": "blob"}
                if change == "D":
                    entry.update(mode=match[1].decode("ascii"), sha=None)
                else:
                    size = int(git("cat-file", "-s", blob))
                    total += size
                    if size > MAX_BLOB or total > MAX_TREE_PAYLOAD:
                        raise ProposalError("The changed files exceed the size limit for a draft proposal.")
                    try:
                        content = git("cat-file", "blob", blob).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ProposalError("Proposed files must be UTF-8 text.") from exc
                    if "\x00" in content:
                        raise ProposalError("Binary files are outside the draft proposal flow.")
                    entry.update(mode=mode, content=content)
                entries.append(entry)
            canonical = git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames",
                            "--full-index", "--no-color", base, "--")
            if len(canonical) > PATCH_LIMIT or not canonical:
                raise ProposalError("The complete public diff exceeds the limit or is empty.")
            try:
                exact = canonical.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProposalError("The complete public diff must be UTF-8 text.") from exc
            if UNSAFE_TEXT.search(exact) or SECRET.search(exact) or b"Binary files " in canonical:
                raise ProposalError("A public diff contains hidden text, binary data or a possible credential.")
            yield exact, entries, git("write-tree").decode("ascii").strip()

    def preview(self, commit):
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise ProposalError("Supply the full lowercase 40-character local commit SHA.")
        payload = self.workspace({"op": "preview", "commit": commit}, None)
        if "error" in payload:
            raise ProposalError(str(payload["error"])[:500])
        base, encoded = payload.get("base"), payload.get("patch_b64")
        if (not isinstance(base, str) or not SHA.fullmatch(base) or payload.get("commit") != commit
                or not isinstance(encoded, str) or len(encoded) > 4 * ((PATCH_LIMIT + 2) // 3)):
            raise ProposalError("The coding workspace did not return a valid bounded proposal.")
        try:
            patch = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ProposalError("The coding workspace returned invalid patch data.") from exc
        with self._staged(base, patch) as (exact, entries, _):
            paths = [entry["path"] for entry in entries]
            subject = paths[0] if len(paths) == 1 else f"{len(paths)} project files"
            evidence = {"repository": REPO, "base_branch": "main", "base": base, "source_commit": commit,
                        "paths": paths, "patch": exact, "title": ("Lantern proposal for " + subject)[:120],
                        "body": "Lantern prepared this change in its coding workspace. Submitted as a draft for maintainer review."}
        identity = proposal_id(evidence)
        dest = self.state / f"{identity}.json"
        if not dest.exists():
            with TemporaryDirectory(dir=self.state) as folder:
                tmp = Path(folder) / "proposal"
                created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                tmp.write_text(json.dumps({"evidence": evidence, "created": created}, ensure_ascii=True),
                               encoding="utf-8")
                os.replace(tmp, dest)
        return self.evidence(identity)

    def _record(self, identity):
        if not isinstance(identity, str) or not PROPOSAL_ID.fullmatch(identity):
            raise ProposalError("Invalid proposal ID. Prepare a preview first.")
        try:
            record = json.loads((self.state / f"{identity}.json").read_text(encoding="utf-8"))
            data = record["evidence"]
            created = datetime.fromisoformat(record["created"])
            if created.tzinfo is None:
                raise ValueError("Expected UTC date")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ProposalError("This proposal preview is unavailable. Prepare a fresh preview.") from exc
        if proposal_id(data) != identity:
            raise ProposalError("The stored proposal does not match its reviewed fingerprint.")
        return {"proposal_id": identity, **data}, created.astimezone(timezone.utc).isoformat()

    def evidence(self, identity):
        return self._record(identity)[0]

    def _request(self, method, path, payload=None):
        return _api(self.token, method, path, payload)

    def open_draft(self, identity):
        packet, created = self._record(identity)
        if not self.token:
            raise ProposalError("Draft publication needs the operator's repository credential. Sync and preview are available.")
        branch = "lantern/proposal-" + identity
        query = urlencode({"state": "all", "head": f"mikepfrank:{branch}", "base": "main", "per_page": 1})
        def existing():
            records = self._request("GET", "pulls?" + query)
            if not isinstance(records, list):
                raise ProposalError("Unexpected GitHub pull request lookup result.")
            return self._pull_receipt(identity, branch, records[0]) if records else None
        prior = existing()
        if prior:
            return prior
        base = packet["base"]
        upstream = self._request("GET", "git/ref/heads/main")
        current_base = upstream.get("object", {}).get("sha")
        if not isinstance(current_base, str) or not SHA.fullmatch(current_base):
            raise ProposalError("GitHub returned an unexpected main branch.")
        published_head = None
        if current_base != base:
            # A previous attempt can have published the branch before its PR response
            # was lost. The reviewed change is still the PR delta if main advanced.
            try:
                ref = self._request("GET", "git/ref/heads/" + branch)
            except ApiError as exc:
                if exc.status == 404:
                    raise ProposalError("GitHub main has moved. Sync, integrate main locally, and prepare a fresh preview.") from exc
                raise
            published_head = ref.get("object", {}).get("sha")
            if not isinstance(published_head, str) or not SHA.fullmatch(published_head):
                raise ProposalError("GitHub returned an unexpected existing proposal branch.")
            self._fetch_main()
            if _git(["rev-parse", BASE_REF], repo=self.mirror).decode().strip() != current_base:
                raise ProposalError("GitHub main changed during proposal recovery. Retry the same ID.")
            _git(["merge-base", "--is-ancestor", base, current_base], repo=self.mirror)
        if _git(["ls-tree", current_base, "--", ".github/workflows"], repo=self.mirror):
            raise ProposalError("The upstream repository now has GitHub workflows. Ask the operator to review automated execution before publishing.")
        with self._staged(base, packet["patch"].encode()) as (exact, entries, tree):
            if exact != packet["patch"] or [e["path"] for e in entries] != packet["paths"]:
                raise ProposalError("The staged change differs from its preview.")
            if published_head:
                commit = self._request("GET", "git/commits/" + published_head)
                if (commit.get("tree", {}).get("sha") != tree or commit.get("message") != packet["title"]
                        or [p.get("sha") for p in commit.get("parents", [])] != [base]):
                    raise ProposalError("The existing proposal branch differs from this preview.")
            else:
                self._publish_head(packet, branch, tree, entries, created)
        try:
            pr = self._request("POST", "pulls", {"title": packet["title"], "body": packet["body"],
                               "head": branch, "base": "main", "draft": True, "maintainer_can_modify": False})
        except ApiError as exc:
            if exc.status == 422 and (prior := existing()):
                return prior
            raise
        return self._pull_receipt(identity, branch, pr)

    def _publish_head(self, packet, branch, tree, entries, created):
        base = packet["base"]
        base_tree = _git(["rev-parse", base + "^{tree}"], repo=self.mirror).decode().strip()
        remote_tree = self._request("POST", "git/trees", {"base_tree": base_tree, "tree": entries})
        if remote_tree.get("sha") != tree:
            raise ProposalError("GitHub's resulting tree differs from the locally verified proposal.")
        # A GitHub noreply address would credit the account that owns the matching username.
        author = {"name": "Lantern", "email": "lantern@lantern-agent.invalid", "date": created}
        # Explicit timestamps keep a retry after an HTTP disconnect on the same commit.
        commit = self._request("POST", "git/commits", {"tree": tree, "parents": [base],
                               "message": packet["title"], "author": author, "committer": author})
        sha = commit.get("sha")
        if (not isinstance(sha, str) or not SHA.fullmatch(sha) or commit.get("tree", {}).get("sha") != tree
                or [p.get("sha") for p in commit.get("parents", [])] != [base]):
            raise ProposalError("GitHub returned an unexpected proposal commit.")
        try:
            self._request("POST", "git/refs", {"ref": "refs/heads/" + branch, "sha": sha})
        except ApiError as exc:
            if exc.status != 422:
                raise
            current = self._request("GET", "git/ref/heads/" + branch)
            if current.get("object", {}).get("sha") != sha:
                raise ProposalError("A different commit already owns this proposal branch.") from exc
    @staticmethod
    def _pull_receipt(identity, branch, pr):
        number = pr.get("number")
        if (type(number) is not int or number <= 0 or pr.get("head", {}).get("ref") != branch
                or pr.get("base", {}).get("ref") != "main" or pr.get("state") not in {"open", "closed"}
                or type(pr.get("draft")) is not bool
                or any(pr.get(key, {}).get("repo", {}).get("full_name") != REPO for key in ("head", "base"))):
            raise ProposalError("GitHub returned an unexpected pull request identity.")
        return {"proposal_id": identity, "branch": branch, "url": f"https://github.com/{REPO}/pull/{number}",
                "state": pr["state"], "draft": pr["draft"]}
