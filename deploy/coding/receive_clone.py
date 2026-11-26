"""Validate a shallow Git pack and publish an editable checkout inside the offline container."""

from contextlib import ExitStack, contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys


PACK_LIMIT = 24 * 1024 * 1024
TREE_LIMIT = 48 * 1024 * 1024
ENTRY_LIMIT = 8192
CLONED, ALREADY_PRESENT, OCCUPIED, BAD_PACK, STORAGE_ERROR = 0, 10, 11, 12, 13
GIT = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.protectNTFS=true",
       "-c", "core.protectHFS=true", "-c", "pack.threads=1", "-c", "core.deltaBaseCacheLimit=8m"]
ENV = {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": "",
       "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}


class BadPack(ValueError):
    pass


@contextmanager
def directory(path, *, parent=None, create=False):
    if create:
        try:
            os.mkdir(path, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, env=ENV, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout


@contextmanager
def git_pipe(cwd, *args, input_pipe=False):
    with subprocess.Popen([*GIT, *args], cwd=cwd, env=ENV,
                          stdin=subprocess.PIPE if input_pipe else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            yield proc
            if input_pipe:
                proc.stdin.close()
            if proc.wait() != 0:
                raise BadPack
        finally:
            if proc.poll() is None:
                proc.kill()


def tree_entries(stage, commit):
    entries = []
    expanded = 0
    pending = b""
    with git_pipe(stage, "ls-tree", "-trlz", "--full-tree", commit) as proc:
        while chunk := proc.stdout.read(65536):
            records = (pending + chunk).split(b"\0")
            pending = records.pop()
            if len(pending) > 1200:
                raise BadPack
            for record in records:
                header, sep, name = record.partition(b"\t")
                if not sep or not 0 < len(name) <= 1024 or len(entries) >= ENTRY_LIMIT:
                    raise BadPack
                parts = name.split(b"/")
                if (len(parts) > 64 or any(len(p) > 255 or p.lower() in (b"", b".", b"..", b".git")
                                           for p in parts)):
                    raise BadPack
                mode, kind, oid, size_text = header.split()
                if not re.fullmatch(b"[0-9a-f]{40}", oid):
                    raise BadPack
                if (mode, kind) in ((b"040000", b"tree"), (b"160000", b"commit")):
                    size = 0
                elif mode in (b"100644", b"100755", b"120000") and kind == b"blob" and size_text.isdigit():
                    size = int(size_text)
                    expanded += size  # Counts each path, including repeated blobs or subtrees.
                    if expanded > TREE_LIMIT:
                        raise BadPack
                else:
                    raise BadPack
                entries.append((os.fsdecode(name), mode, oid, size))
        if pending:
            raise BadPack
    return entries


def write_tree(stage, entries):
    # Raw blob materialization avoids content filters, encoding conversions,
    # hooks and recursive submodule checkout. Symlink blobs become plain text.
    with git_pipe(stage, "cat-file", "--batch", input_pipe=True) as proc:
        for name, mode, oid, size in entries:
            path = stage / name
            if mode in (b"040000", b"160000"):
                path.mkdir()
                continue
            proc.stdin.write(oid + b"\n")
            proc.stdin.flush()
            if proc.stdout.readline(100) != oid + b" blob " + str(size).encode() + b"\n":
                raise BadPack
            with path.open("xb") as file:
                remaining = size
                while remaining:
                    chunk = proc.stdout.read(min(65536, remaining))
                    if not chunk:
                        raise BadPack
                    file.write(chunk)
                    remaining -= len(chunk)
            if proc.stdout.read(1) != b"\n":
                raise BadPack
            if mode == b"100755":
                path.chmod(0o755)


def checkout(stage, pack, commit, count, pack_id):
    git(stage, "init", "--template=", "--initial-branch=study", ".")
    dotgit = stage / ".git"
    pack_path = dotgit / "objects" / "pack" / ("pack-" + pack_id + ".pack")
    pack.rename(pack_path)
    (dotgit / "shallow").write_text(commit + "\n", encoding="ascii")
    git(stage, "index-pack", "--threads=1", str(pack_path))
    if git(stage, "cat-file", "-t", commit) != b"commit\n":
        raise BadPack
    objects = git(stage, "cat-file", "--batch-all-objects", "--batch-check=%(objecttype) %(objectsize)").splitlines()
    if len(objects) != count:
        raise BadPack
    object_bytes = 0
    for line in objects:
        kind, size = line.split()
        if kind not in (b"commit", b"tree", b"blob") or not size.isdigit():
            raise BadPack
        object_bytes += int(size)
    # Allow bounded commit and tree metadata in addition to expanded files.
    if object_bytes > TREE_LIMIT + 4 * 1024 * 1024:
        raise BadPack
    git(stage, "fsck", "--strict", "--full", "--no-dangling", "--no-reflogs", commit)
    if len(git(stage, "rev-list", "--objects", "--no-object-names", commit).splitlines()) != count:
        raise BadPack  # The payload must contain only objects reachable from this shallow revision.
    entries = tree_entries(stage, commit)
    git(stage, "config", "core.symlinks", "false")
    git(stage, "update-ref", "refs/heads/study", commit)
    git(stage, "read-tree", commit)
    write_tree(stage, entries)


def save_pack(stream, staging, digest):
    with (staging / "incoming.pack").open("xb") as file:
        checksum = hashlib.sha256()
        total = 0
        while chunk := stream.read(65536):
            total += len(chunk)
            if total > PACK_LIMIT:
                raise BadPack
            checksum.update(chunk)
            file.write(chunk)
    pack = staging / "incoming.pack"
    if total < 32 or checksum.hexdigest() != digest:
        raise BadPack
    with pack.open("rb") as file:
        signature, version, count = struct.unpack(">4sII", file.read(12))
        file.seek(-20, os.SEEK_END)
        pack_id = file.read(20).hex()
    if signature != b"PACK" or version not in (2, 3) or not 1 <= count <= ENTRY_LIMIT + 2:
        raise BadPack
    return pack, count, pack_id


def read_existing(parent, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            return None
        with os.fdopen(fd, "rb", closefd=False) as file:
            return file.read(limit + 1)
    finally:
        os.close(fd)


def existing(parent, repo, commit, receipt):
    # Only the offline importer can write this receipt. It binds both the
    # published directory and its retained original pack, even when GitHub
    # subsequently compresses the same approved revision differently.
    try:
        origin = json.loads(receipt.read_bytes())
        digest = origin["sha256"]
        pack_id = origin["pack_id"]
        if (origin["repo"] != repo or origin["commit"] != commit
                or not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(r"[0-9a-f]{40}", pack_id)):
            return None
        marker = (repo + "\n" + commit + "\n" + digest + "\n").encode("ascii")
        with ExitStack() as stack:
            dest = stack.enter_context(directory(commit, parent=parent))
            info = os.fstat(dest)
            if origin["inode"] != info.st_ino or info.st_dev != receipt.stat().st_dev:
                return None
            dotgit = stack.enter_context(directory(".git", parent=dest))
            if read_existing(dotgit, "lantern-origin", 512) != marker:
                return None
            objects = stack.enter_context(directory("objects", parent=dotgit))
            packs = stack.enter_context(directory("pack", parent=objects))
            data = read_existing(packs, "pack-" + pack_id + ".pack", PACK_LIMIT)
            if data is not None and hashlib.sha256(data).hexdigest() == digest:
                return digest
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def publish(stage, parent, commit):
    # Linux renameat2 provides no-replace even if an existing directory is empty.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(stage), parent, commit.encode("ascii"), 1) < 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            return False
        raise OSError(number, os.strerror(number))
    os.fsync(parent)
    return True


def receive(stream, volume, repo, commit, digest):
    import fcntl
    if (repo.endswith((".", ".git")) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?/[a-z0-9_][a-z0-9._-]{0,99}", repo)
            or not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        return BAD_PACK
    staging = volume / "clone-stage"
    with directory(staging) as private, ExitStack() as stack:
        fcntl.flock(private, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only this fixed helper has a mount of the staging area. A process
        # killed during a previous handoff could have left a partial checkout.
        stage = staging / "checkout"
        pack = staging / "incoming.pack"
        if stage.exists():
            shutil.rmtree(stage)
        pack.unlink(missing_ok=True)
        stage.mkdir(mode=0o700)
        stack.callback(shutil.rmtree, stage, True)
        stack.callback(pack.unlink, missing_ok=True)
        try:
            pack, count, pack_id = save_pack(stream, staging, digest)
            parent = stack.enter_context(directory(volume / "files"))
            try:
                for part in ("repos", *repo.split("/")):
                    parent = stack.enter_context(directory(part, parent=parent, create=True))
            except (FileExistsError, NotADirectoryError):
                return OCCUPIED
            marker = (repo + "\n" + commit + "\n" + digest + "\n").encode("ascii")
            key = hashlib.sha256((repo + "\n" + commit).encode("ascii")).hexdigest()
            receipt = staging / (key + ".json")
            try:
                os.stat(commit, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                original = existing(parent, repo, commit, receipt)
                if not original:
                    return OCCUPIED
                if original != digest:
                    checkout(stage, pack, commit, count, pack_id)
                return ALREADY_PRESENT
            checkout(stage, pack, commit, count, pack_id)
            (stage / ".git" / "lantern-origin").write_bytes(marker)
            # Persist the recipient identity before the no-replace rename. A
            # killed transfer thus leaves either a recoverable published copy
            # or an absent destination which a retry can safely populate.
            info = stage.stat()
            origin = {"repo": repo, "commit": commit, "sha256": digest, "pack_id": pack_id, "inode": info.st_ino}
            temp = staging / "pending-receipt"
            try:
                with temp.open("w", encoding="ascii") as file:
                    json.dump(origin, file)
                    file.flush()
                    os.fsync(file.fileno())
                temp.replace(receipt)
                os.fsync(private)
            finally:
                temp.unlink(missing_ok=True)
            if publish(stage, parent, commit):
                return CLONED
            return OCCUPIED
        except (ValueError, subprocess.CalledProcessError):
            return BAD_PACK


if __name__ == "__main__":
    try:
        outcome = receive(sys.stdin.buffer, Path("/volume"), *sys.argv[1:])
    except OSError:
        outcome = STORAGE_ERROR
    sys.exit(outcome)
