"""Render one page in the offline PDF container. Paths are container-local."""

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


MAX_DOCUMENT = 64 * 1024 * 1024
MAX_IMAGE = 1024 * 1024
MAX_SIDE = 1600


@contextmanager
def open_document(path):
    # The two container configurations expose either the fixed private file or
    # /work. Pin every component so a symlink, FIFO or device cannot act as a PDF.
    parts = Path(path).parts
    if path != "/document.pdf" and (parts[:2] != ("/", "work") or ".." in parts or len(parts) < 3):
        raise ValueError("Invalid PDF path.")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_DOCUMENT:
            raise ValueError("Choose a nonempty regular PDF of at most 64 MiB.")
        yield fd
    finally:
        os.close(fd)


def render(fd, page):
    source = f"/proc/self/fd/{fd}"
    common = dict(stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(fd,), check=True)
    with tempfile.TemporaryDirectory() as temp:
        # Never read a large PDF metadata field into memory. The read-only
        # container also bounds temporary output via tmpfs and RLIMIT_FSIZE.
        with tempfile.TemporaryFile() as info:
            subprocess.run(["/usr/bin/pdfinfo", source], stdout=info, timeout=8, **common)
            if info.tell() > 128 * 1024:
                raise ValueError("PDF metadata exceeds the supported size.")
            info.seek(0)
            counts = re.findall(rb"^Pages:[ \t]+([0-9]{1,9})[ \t]*$", info.read(), re.MULTILINE)
        if not counts:
            raise ValueError("Unable to read the PDF page count.")
        count = int(counts[-1])
        if not 1 <= page <= count:
            raise ValueError(f"This PDF has {count} pages. Use a one-based page number within that range.")
        prefix = Path(temp) / "page"
        png = prefix.with_suffix(".png")
        side = MAX_SIDE
        while True:
            subprocess.run(["/usr/bin/pdftoppm", "-f", str(page), "-l", str(page), "-cropbox", "-singlefile",
                            "-scale-to", str(side), "-png", source, str(prefix)],
                           stdout=subprocess.DEVNULL, timeout=18, **common)
            size = png.stat().st_size
            if 0 < size <= MAX_IMAGE:
                return {"page_count": count, "png": base64.b64encode(png.read_bytes()).decode("ascii")}
            if side <= 512 or not size:
                raise ValueError("Cannot render this page within the image size limit.")
            # Photographs often need fewer pixels than line or text drawings.
            side = max(512, int(side * min(0.70, (0.85 * MAX_IMAGE / size) ** 0.5)))


def main():
    try:
        with open_document(sys.argv[1]) as fd:
            result = render(fd, int(sys.argv[2]))
    except (OSError, subprocess.CalledProcessError):
        result = {"error": "Cannot open or render this PDF. Check the path and use an accessible, unencrypted PDF."}
    except subprocess.TimeoutExpired:
        result = {"error": "The PDF exceeded the page rendering time limit."}
    except ValueError as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
