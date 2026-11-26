"""Download a named operator-granted data file without using ambient credentials."""

import csv
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit

from .page_fetch import MAX_REDIRECTS, PageFetchError, _checked_url, _PublicConnection


MAX_BYTES = 16 * 1024 * 1024
MAX_IMPORT_URL = 2048
DEADLINE_SECONDS = 30
_CHUNK = 64 * 1024
_JSON_BYTES = 2 * 1024 * 1024  # The standard JSON decoder materializes the entire tree.
_TABULAR_LINE = 256 * 1024
_EXTENSIONS = (".txt", ".csv", ".tsv", ".json", ".pdf", ".tar", ".tar.gz", ".tgz")
SUPPORTED_FORMATS = ", ".join(_EXTENSIONS) + ", or numbered tar archive parts"
_ARCHIVE_PART = re.compile(r"\.(?:tar(?:\.gz)?|tgz)\.(?:part[0-9]{2,6}|[0-9]{3})$", re.I)
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ACTIVE_PREFIX = re.compile(
    r"(?:#!|<!(?:doctype\b|--)|<\?|<(?:html|head|body|script|svg|iframe|meta|style|link|form|embed|object)\b"
    r"|@echo\s+off\b)", re.I)
_ACTIVE_MIME = {"application/javascript", "text/javascript", "text/html", "application/xhtml+xml",
                "image/svg+xml", "application/x-sh", "text/x-python", "application/x-executable"}


class ImportFetchError(ValueError):
    """A diagnostic suitable for a tool receipt."""


class ImportRedirectNeeded(ImportFetchError):
    """An exact, validated mirror that would need a new operator decision."""

    def __init__(self, url):
        self.url = checked_import_url(url)
        super().__init__("The data server redirected to a URL that needs approval.")


def checked_filename(filename):
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", filename):
        raise ImportFetchError("Use a plain filename of 1–120 characters starting with a letter or digit. "
                               "Use only ASCII letters, digits, dots, hyphens and underscores.")
    name = filename.lower()
    if not name.endswith(_EXTENSIONS) and not _ARCHIVE_PART.search(name):
        suffix = Path(filename).suffix
        reason = f"Unsupported import extension '{suffix}'." if suffix else "The import filename needs an extension."
        raise ImportFetchError(f"{reason} Supported formats include {SUPPORTED_FORMATS}.")
    return filename


def valid_filename(filename):
    try:
        checked_filename(filename)
        return True
    except ImportFetchError:
        return False


def checked_import_url(url):
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_IMPORT_URL:
        raise ImportFetchError("Each public source URL must fit in 2048 characters.")
    if "#" in url or re.search(r"%(?![0-9A-Fa-f]{2})", url):
        raise ImportFetchError("Supply a public HTTPS file URL without fragments or malformed escapes.")
    try:
        normalized = _checked_url(url)
    except PageFetchError as exc:
        raise ImportFetchError(str(exc)) from None
    if len(normalized) > MAX_IMPORT_URL:
        raise ImportFetchError("The encoded source URL exceeds 2048 characters.")
    return normalized


def _copy_body(response, output, limit):
    def header(name, default=None):
        values = response.headers.get_all(name, [])
        if len(values) > 1:
            raise ImportFetchError("The data server returned ambiguous response headers.")
        return values[0].strip() if values else default

    if header("Content-Encoding", "identity").lower() != "identity":
        raise ImportFetchError("The data server returned an unsupported content encoding.")
    if header("Content-Type", "").split(";", 1)[0].strip().lower() in _ACTIVE_MIME:
        raise ImportFetchError("The data server returned an active document or program content type.")
    length = header("Content-Length")
    transfer = header("Transfer-Encoding")
    if header("Content-Range") is not None or (transfer is not None and
                                               (transfer.lower() != "chunked" or length is not None)):
        raise ImportFetchError("The data server returned unsupported or ambiguous response framing.")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,16}", length):
            raise ImportFetchError("The data server returned an invalid content length.")
        length = int(length)
        if length > limit:
            raise ImportFetchError("The file exceeds the approved byte limit.")
    total = 0
    while chunk := response.read(min(_CHUNK, limit - total + 1)):
        total += len(chunk)
        if total > limit:
            raise ImportFetchError("The file exceeds the approved byte limit.")
        output.write(chunk)
    if length is not None and total != length:
        raise ImportFetchError("The data file transfer was incomplete.")


def _fetch(url, redirect_urls, output, limit):
    visited = {url}
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        target = urlunsplit(("", "", parts.path, parts.query, ""))
        with closing(_PublicConnection(parts.hostname, port=443, timeout=8)) as connection:
            connection.request("GET", target, headers={"Accept": "*/*", "Accept-Encoding": "identity",
                               "User-Agent": "Lantern-Data-Import/1.0", "Connection": "close"})
            with connection.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    locations = response.headers.get_all("Location", [])
                    location = locations[0] if len(locations) == 1 else ""
                    if (not location or len(location) > MAX_IMPORT_URL or "#" in location or "\\" in location
                            or not location.isprintable() or any(c.isspace() for c in location)):
                        raise ImportFetchError("The data server returned an invalid redirect.")
                    url = checked_import_url(urljoin(url, location))
                    if url in visited or hop == MAX_REDIRECTS:
                        raise ImportFetchError("The data server returned a repeated or excessive redirect.")
                    if url not in redirect_urls:
                        raise ImportRedirectNeeded(url)
                    visited.add(url)
                    continue
                if response.status != 200:
                    raise ImportFetchError(f"The data server returned HTTP {response.status:d}.")
                _copy_body(response, output, limit)
                return url


def _child():
    try:
        url, redirects, limit = json.load(sys.stdin)
        result = {"url": _fetch(url, redirects, sys.stdout.buffer, limit)}
        sys.stdout.buffer.flush()
    except ImportRedirectNeeded as exc:
        result = {"redirect": exc.url}
    except (ImportFetchError, PageFetchError) as exc:
        result = {"error": str(exc)}
    except ssl.SSLCertVerificationError:
        result = {"error": "The data server's HTTPS certificate could not be verified."}
    except (ValueError, OSError, http.client.HTTPException):
        result = {"error": "The data transfer failed or was incomplete."}
    print(json.dumps(result, ensure_ascii=True), file=sys.stderr)


def fetch_import(url, redirect_urls, destination, max_bytes):
    """Write raw bytes to a private temporary file. Return the verified final URL."""
    url = checked_import_url(url)
    if (not isinstance(redirect_urls, (list, tuple)) or len(redirect_urls) > MAX_REDIRECTS
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES):
        raise ImportFetchError("The import grant has invalid redirects or byte limits.")
    redirects = [checked_import_url(item) for item in redirect_urls]
    # stdout is the data file; the child receives no destination path, secrets, or proxy settings.
    # A subprocess deadline bounds DNS resolution and trickling HTTP headers as well as the body.
    runner = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from lantern.import_fetch import _child; _child()")
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    try:
        with open(destination, "wb") as output:
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-c", runner, str(Path(__file__).resolve().parent.parent)],
                    input=json.dumps([url, redirects, max_bytes]).encode("ascii"), stdout=output,
                    stderr=subprocess.PIPE, timeout=DEADLINE_SECONDS, env=env, check=True)
                result = json.loads(completed.stderr)
                if not isinstance(result, dict) or len(result) != 1:
                    raise ImportFetchError("The data transfer returned invalid metadata.")
                if "redirect" in result:
                    needed = ImportRedirectNeeded(result["redirect"])
                    if needed.url in (url, *redirects):
                        raise ImportFetchError("The data transfer returned an unexpected redirect.")
                    raise needed
                if "error" in result:
                    raise ImportFetchError(result["error"])
                if result.get("url") not in (url, *redirects):
                    raise ImportFetchError("The data transfer returned an unexpected final URL.")
                return result["url"]
            except Exception:
                output.truncate(0)
                raise
    except ImportFetchError:
        raise
    except subprocess.TimeoutExpired:
        raise ImportFetchError("The file exceeded the retrieval deadline.") from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ImportFetchError("The data file could not be retrieved.") from None


def _check_archive(path, name, size):
    # Split archives are opaque chunks and have no per-part magic. No archive is extracted.
    if _ARCHIVE_PART.search(name):
        return
    with path.open("rb") as source:
        head = source.read(1024)
    if name.endswith((".tgz", ".tar.gz")):
        if size < 18 or head[:3] != b"\x1f\x8b\x08" or head[3] & 0xe0:
            raise ImportFetchError("Expected a gzip archive header.")
        return
    if size >= 1024 and size % 512 == 0:
        if head == bytes(1024):
            return
        block = head[:512]
        checksum = block[148:156].strip(b" \0")
        if re.fullmatch(b"[0-7]+", checksum) and int(checksum, 8) == sum(block[:148] + b" " * 8 + block[156:]):
            return
    raise ImportFetchError("Expected a tar archive header.")


def _reject_json_constant(_value):
    raise ImportFetchError("Expected valid JSON data.")


def _check_pdf(path, size):
    # Recognize the file envelope only. Object parsing belongs to the offline viewer.
    with path.open("rb") as source:
        if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n]", source.read(9)):
            raise ImportFetchError("Expected a PDF version header at the start of the downloaded file.")
        source.seek(max(0, size - 4096))
        trailer = re.search(rb"[\r\n]startxref[ \t\r\n]+([0-9]{1,20})[ \t\r\n]+%%EOF[ \t\r\n]*\Z", source.read(4096))
    if not trailer or not 0 < int(trailer[1]) < size:
        raise ImportFetchError("Expected a complete PDF with a final startxref offset and %%EOF marker.")


def _table_lines(stream):
    while line := stream.readline(_TABULAR_LINE + 1):
        if len(line) > _TABULAR_LINE:
            raise ImportFetchError("A table line exceeds the inspection limit.")
        yield line


def validate_data(path, filename):
    """Check the declared format without changing any bytes or unpacking archives."""
    checked_filename(filename)
    path = Path(path)
    name = filename.lower()
    try:
        size = path.stat().st_size
        if not 1 <= size <= MAX_BYTES:
            raise ImportFetchError("The data file is empty or exceeds the import size limit.")
        if name.endswith(".pdf"):
            _check_pdf(path, size)
            return
        if name.endswith((".tar", ".tgz", ".tar.gz")) or _ARCHIVE_PART.search(name):
            _check_archive(path, name, size)
            return
        with path.open(encoding="utf-8-sig", newline="") as source:
            prefix = ""
            while chunk := source.read(_CHUNK):
                if _CONTROLS.search(chunk):
                    raise ImportFetchError("Text data contains binary or control characters.")
                if len(prefix) < 512:
                    sample = chunk if prefix else chunk.lstrip()
                    prefix += sample[:512 - len(prefix)]
            if not prefix or _ACTIVE_PREFIX.match(prefix):
                raise ImportFetchError("Expected plain data instead of a program, web document, or blank file.")
            source.seek(0)
            if name.endswith(".json"):
                if size > _JSON_BYTES:
                    raise ImportFetchError("JSON files must be at most 2 MiB for inspection.")
                json.load(source, parse_constant=_reject_json_constant)
            elif name.endswith((".csv", ".tsv")):
                delimiter = "," if name.endswith(".csv") else "\t"
                for _ in csv.reader(_table_lines(source), delimiter=delimiter, strict=True):
                    pass
    except ImportFetchError:
        raise
    except (OSError, ValueError, csv.Error, RecursionError):
        raise ImportFetchError("The file does not contain valid data in the declared format and encoding.") from None
