"""Read bounded quotations from the operator-provisioned private LM Studio archive."""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import stat


MARKDOWN = "lmstudio-chat.md"
RAW_TEXT = "lmstudio-chat.txt"
PDF = "lmstudio-chat.pdf"
PDF_PAGES = "lmstudio-chat.pdf-pages.json"
FILES = (MARKDOWN, RAW_TEXT, PDF)
DEFAULT_LENGTH = 2400
MAX_LENGTH = 4000
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024

HERITAGE_TOOL = {
    "name": "read_heritage",
    "description": (
        "Read a quotation from Mike and Qwen's private LM Studio design conversation. "
        "Use the Markdown transcript by default. TXT displays escaped message delimiters. "
        "For the illustrated PDF, select a page to read its text. Use next_offset to continue reading."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "enum": list(FILES), "default": MARKDOWN},
            "page": {"type": "integer", "minimum": 1,
                     "description": "One-based PDF page number. Defaults to 1 for a PDF."},
            "offset": {"type": "integer", "minimum": 0, "default": 0,
                       "description": "Starting character within the text document or PDF page, counting from zero."},
            "length": {"type": "integer", "minimum": 1, "maximum": MAX_LENGTH, "default": DEFAULT_LENGTH},
        },
        "required": [],
        "additionalProperties": False,
    },
}


@contextmanager
def _open_document(directory, name):
    root = Path(directory).absolute()
    path = root / name
    if root.resolve(strict=True) != root:
        raise ValueError("The configured heritage folder must be a real directory.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    expected = None
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        # Pin the directory on Linux. A replacement of the directory or leaf
        # cannot redirect the read to a symlink target.
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    else:
        if path.resolve(strict=True) != path:
            raise ValueError("The heritage document must be an ordinary file in the configured folder.")
        expected = path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError("The heritage document must be a regular file.")
        fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("The heritage document must be a regular file.")
        if expected is not None and (not os.path.samestat(expected, opened) or path.resolve(strict=True) != path):
            raise ValueError("The heritage document changed while opening. Try again.")
        with open(fd, "rb", closefd=False) as source:
            yield source, path, opened.st_size
    finally:
        os.close(fd)


def _pdf_page(directory, pdf, size, page):
    if size > MAX_PDF_BYTES:
        raise ValueError("The heritage PDF exceeds its supported size.")
    with _open_document(directory, PDF_PAGES) as (index, _, index_size):
        if index_size > MAX_TEXT_BYTES:
            raise ValueError("The heritage PDF text index exceeds its supported size.")
        raw = index.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("The heritage PDF text index exceeds its supported size.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("The heritage PDF text index is invalid.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        raise ValueError("The heritage PDF text index is invalid.")
    pages = data["pages"]
    if not 1 <= len(pages) <= 4096 or not all(isinstance(text, str) for text in pages):
        raise ValueError("The heritage PDF text index is invalid.")
    if hashlib.file_digest(pdf, "sha256").hexdigest() != data.get("sha256"):
        raise ValueError("The heritage PDF text index does not match the archived PDF.")
    if page > len(pages):
        raise ValueError(f"The heritage PDF has {len(pages)} pages.")
    return pages[page - 1], len(pages)


def read_heritage(directory: Path, arguments: dict) -> dict:
    """Return a passage with a continuation cursor and information about its source."""
    if not isinstance(arguments, dict) or set(arguments) - {"filename", "offset", "length", "page"}:
        raise ValueError("read_heritage accepts filename, offset, length and PDF page.")
    name = arguments.get("filename", MARKDOWN)
    if name not in FILES:
        raise ValueError("Choose one of the three heritage filenames.")
    offset, length = arguments.get("offset", 0), arguments.get("length", DEFAULT_LENGTH)
    page = arguments.get("page", 1)
    if type(page) is not int or page < 1 or ("page" in arguments and name != PDF):
        raise ValueError("Use a positive page number with the PDF edition.")
    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MAX_LENGTH:
        raise ValueError(f"Use a nonnegative character offset and a length of 1 to {MAX_LENGTH}.")
    try:
        with _open_document(directory, name) as (source, path, size):
            result = {"filename": name, "path": str(path), "size_bytes": size,
                      "source": "Private historical LM Studio conversation between Mike and Qwen."}
            if name == PDF:
                text, count = _pdf_page(directory, source, size, page)
                if offset > len(text):
                    raise ValueError("The requested offset is past the end of the PDF page.")
                passage = text[offset:offset + length + 1]
                result.update(page=page, page_count=count, text_companion=MARKDOWN,
                              note="Extracted page text. The archived PDF preserves the images for a visual viewer.")
            else:
                # Decode UTF-8 without splitting a character. Normalize BOM and line
                # endings across exports and skip to the cursor in fixed-memory chunks.
                if size > MAX_TEXT_BYTES:
                    raise ValueError("The heritage transcript exceeds its supported size.")
                if offset > size:
                    raise ValueError("The requested offset is past the end of the document.")
                with io.TextIOWrapper(source, encoding="utf-8-sig", newline=None) as reader:
                    remaining = offset
                    while remaining:
                        skipped = reader.read(min(remaining, 65536))
                        if not skipped:
                            raise ValueError("The requested offset is past the end of the document.")
                        remaining -= len(skipped)
                    passage = reader.read(length + 1)
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise ValueError("The requested heritage document could not be read.") from exc
    text = passage[:length]
    result.update(offset=offset, next_offset=offset + length if len(passage) > length else None)
    if name == RAW_TEXT:
        # Keep role markers readable without reproducing tokenizer control sequences.
        text = text.translate(str.maketrans({"<": r"\u003c", ">": r"\u003e", "[": r"\u005b", "]": r"\u005d"}))
        result["rendering"] = "Angle and square brackets use literal Unicode escapes in this quotation."
    return {**result, "quotation": text}
