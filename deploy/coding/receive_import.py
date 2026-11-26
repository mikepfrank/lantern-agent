"""Receive one verified data object inside Lantern's offline coding container."""

import hashlib
import os
import re
import stat
import sys


LIMIT = 16 * 1024 * 1024
CHUNK = 65536
IMPORTED, ALREADY_PRESENT, OCCUPIED, BAD_DATA, STORAGE_ERROR = 0, 10, 11, 12, 13


def existing_status(directory, name, digest):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError:
        return OCCUPIED
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT:
        os.close(fd)
        return OCCUPIED
    with os.fdopen(fd, "rb") as existing:
        content = existing.read(LIMIT + 1)
    if len(content) <= LIMIT and hashlib.sha256(content).hexdigest() == digest:
        return ALREADY_PRESENT
    return OCCUPIED


def receive(stream, directory, name, digest):
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", name)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        return BAD_DATA
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    parent = os.open(directory, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
    try:
        # The workspace is mounted ext4. An unnamed temporary inode disappears
        # even if the container is killed during delivery. linkat publishes the
        # verified bytes in one step and never replaces an existing entry.
        fd = os.open(".", os.O_WRONLY | os.O_TMPFILE, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as target:
            checksum = hashlib.sha256()
            count = 0
            while chunk := stream.read(CHUNK):
                count += len(chunk)
                if count > LIMIT:
                    return BAD_DATA
                checksum.update(chunk)
                target.write(chunk)
            if checksum.hexdigest() != digest:
                return BAD_DATA
            target.flush()
            os.fsync(fd)
            try:
                os.link(f"/proc/self/fd/{fd}", name, dst_dir_fd=parent, follow_symlinks=True)
            except FileExistsError:
                return existing_status(parent, name, digest)
            os.fsync(parent)
            return IMPORTED
    finally:
        os.close(parent)


if __name__ == "__main__":
    try:
        status = receive(sys.stdin.buffer, "/work/imports", *sys.argv[1:])
    except OSError:
        status = STORAGE_ERROR
    sys.exit(status)
