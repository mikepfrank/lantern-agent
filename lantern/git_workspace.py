"""Fixed Git operations run inside the offline container, with no host credentials."""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys


WORKTREE = Path("/work/lantern")
UPSTREAM_BUNDLE = Path("/input/upstream.bundle")
UPSTREAM_REF = "refs/remotes/upstream/main"
PATCH_LIMIT = 12 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge",
               "sequencer", "BISECT_START", "index.lock")


class WorkspaceError(ValueError):
    pass


class WorkspaceGit:
    def __init__(self, worktree):
        self.worktree = Path(worktree)
        if not (self.worktree / ".git").is_dir() or self.worktree.is_symlink():
            raise WorkspaceError("The editable project checkout is unavailable.")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1",
                        GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        self.command = ["git", "--git-dir=.git", "--work-tree=.", "-c", f"core.hooksPath={os.devnull}",
                        "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
                        "-c", "core.quotePath=true", "-c", "gc.auto=0"]

    def run(self, *args, check=True):
        result = subprocess.run([*self.command, *args], cwd=self.worktree, env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if check and result.returncode:
            raise WorkspaceError(f"Git could not complete {args[0]} in the editable checkout.")
        return result

    def revision(self, ref):
        result = self.run("rev-parse", "--verify", ref + "^{commit}", check=False)
        value = result.stdout.decode("ascii", "replace").strip()
        if result.returncode or not SHA.fullmatch(value):
            raise WorkspaceError("The requested commit is unavailable. Sync upstream and check the commit ID.")
        return value

    def ancestor(self, old, new):
        code = self.run("merge-base", "--is-ancestor", old, new, check=False).returncode
        if code not in (0, 1):
            raise WorkspaceError("Git could not verify the commit ancestry.")
        return code == 0

    def sync(self, bundle):
        head = self.revision("HEAD")
        symbolic = self.run("symbolic-ref", "--quiet", "HEAD", check=False)
        if symbolic.returncode not in (0, 1):
            raise WorkspaceError("Git could not verify the checked-out branch.")
        ref = symbolic.stdout.decode("utf-8", "replace").strip()
        if ref and not ref.startswith("refs/heads/"):
            raise WorkspaceError("HEAD must be detached or point to a local branch.")
        # Read the supplied bundle directly so local remotes and URL rewrites cannot
        # change the source. An unchanged main needs no additional pack on disk.
        header = self.run("bundle", "list-heads", str(bundle), "refs/heads/main").stdout.strip()
        match = re.fullmatch(rb"([0-9a-f]{40}) refs/heads/main", header)
        if not match:
            raise WorkspaceError("The upstream bundle does not contain a unique main branch.")
        wanted = match[1].decode("ascii")
        if self.run("cat-file", "-e", wanted + "^{commit}", check=False).returncode:
            self.run("bundle", "unbundle", str(bundle))
        upstream = self.revision(wanted)
        self.run("update-ref", "--no-deref", UPSTREAM_REF, upstream)
        result = {"upstream": upstream, "head": head, "branch": ref.removeprefix("refs/heads/") or None}
        if not ref:
            status = "detached"
        elif any(os.path.lexists(self.worktree / ".git" / marker) for marker in IN_PROGRESS):
            status = "blocked"
        elif self.run("status", "--porcelain=v1", "-z", "--untracked-files=normal",
                      "--ignore-submodules=none").stdout:
            status = "dirty"
        elif head == upstream:
            status = "up_to_date"
        elif self.ancestor(upstream, head):
            status = "local_ahead"
        elif not self.ancestor(head, upstream):
            status = "diverged"
        else:
            # Git status can hide changes marked assume-unchanged or skip-worktree.
            index = self.run("ls-files", "-v", "-z").stdout.split(b"\0")
            if any(item[:1].islower() or item.startswith(b"S ") for item in index):
                status = "blocked"
            else:
                merged = self.run("merge", "--ff-only", "--no-squash", "--no-autostash", "--no-overwrite-ignore",
                                  "--no-edit", "--no-stat", upstream, check=False)
                result["head"] = self.revision("HEAD")
                status = "fast_forwarded" if not merged.returncode and result["head"] == upstream else "blocked"
        return {**result, "status": status}

    def preview(self, commit):
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise WorkspaceError("Supply the full 40-character lowercase Git commit ID.")
        base = self.revision(UPSTREAM_REF)
        if self.revision(commit) != commit or not self.ancestor(base, commit):
            raise WorkspaceError("The proposed commit must descend from the synced upstream main.")
        args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-relative",
                "--full-index", "--ignore-submodules=none", "--submodule=short",
                "--src-prefix=a/", "--dst-prefix=b/", base, commit, "--"]
        # Bound the output while Git is producing it. The host reviews the exact
        # patch against its trusted base before any submission to GitHub.
        with subprocess.Popen([*self.command, *args], cwd=self.worktree, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            patch = proc.stdout.read(PATCH_LIMIT + 1)
            if len(patch) > PATCH_LIMIT:
                proc.kill()
                raise WorkspaceError("The proposed diff exceeds 12 KiB. Submit a smaller change.")
            if proc.wait():
                raise WorkspaceError("Git could not produce the proposed diff.")
        if not patch:
            raise WorkspaceError("The proposed commit has no changes relative to synced upstream main.")
        return {"base": base, "commit": commit, "patch_b64": base64.b64encode(patch).decode("ascii")}


def process(request, worktree=WORKTREE, bundle=UPSTREAM_BUNDLE):
    if isinstance(request, dict):
        if set(request) == {"op"} and request["op"] == "sync":
            return WorkspaceGit(worktree).sync(bundle)
        if set(request) == {"op", "commit"} and request["op"] == "preview":
            return WorkspaceGit(worktree).preview(request["commit"])
    raise WorkspaceError("Unknown Git workspace operation.")


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(513)
        if len(raw) > 512:
            raise WorkspaceError("Invalid Git workspace request.")
        reply = process(json.loads(raw))
    except WorkspaceError as exc:
        reply = {"error": str(exc)}
    except (OSError, ValueError, RecursionError):
        reply = {"error": "The Git workspace request could not be processed."}
    print(json.dumps(reply, ensure_ascii=True, separators=(",", ":")))
