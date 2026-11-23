"""Fetch a bounded public page without ambient credentials or proxy settings."""

import codecs
from contextlib import closing
from dataclasses import asdict, dataclass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit


MAX_URL = 4096
MAX_BODY = 512 * 1024
MAX_REDIRECTS = 3
DEADLINE_SECONDS = 18
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SPECIAL_SUFFIXES = {"localhost", "local", "internal", "localdomain", "lan", "arpa", "onion", "invalid", "test", "alt"}
_GLOBAL_V6 = ipaddress.ip_network("2000::/3")
_RETIRED_6TO4_RELAY = ipaddress.ip_network("192.88.99.0/24")
_SPECIAL_V6 = tuple(map(ipaddress.ip_network, ("2001::/23", "2002::/16", "3fff::/20")))
_CREDENTIAL_FIELD = re.compile(
    r"(?:api_?key|(?:access_|id_|refresh_|auth_?)?token|password|secret|client_secret|"
    r"authorization|credential|sig|signature|x_amz_.+|x_goog_.+)", re.I)
_CREDENTIAL_VALUE = re.compile(r"(?:github_pat_|gh[pousr]_|sk-(?:or-|ant-)?)[\w-]{18,}|(?:AKIA|ASIA)[A-Z0-9]{16}")


class PageFetchError(ValueError):
    """A message safe to return to the caller."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    content_type: str
    text: str


def _public_ip(address):
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local:
        return False
    # Some supported Python versions classify special-purpose, transition
    # and documentation ranges as global. Only allow native public web hosts.
    if ip.version == 6:
        return ip in _GLOBAL_V6 and not any(ip in network for network in _SPECIAL_V6)
    return ip not in _RETIRED_6TO4_RELAY


def _checked_url(url):
    if (not isinstance(url, str) or not 1 <= len(url) <= MAX_URL or "\\" in url
            or not url.isprintable() or any(char.isspace() for char in url)):
        raise PageFetchError("Supply a public HTTPS URL without whitespace or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise PageFetchError("Invalid public HTTPS URL.") from None
    if (parts.scheme != "https" or not host or len(host) > 253 or "%" in host
            or parts.username is not None or port not in (None, 443)):
        raise PageFetchError("Use public HTTPS on port 443 without embedded credentials.")
    if (any(_CREDENTIAL_FIELD.fullmatch(key.replace("-", "_")) for key, _ in
            parse_qsl(parts.query, keep_blank_values=True)) or _CREDENTIAL_VALUE.search(unquote(url))):
        raise PageFetchError("Use a public page URL without credentials or signatures.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (len(labels) < 2 or labels[-1].isdigit() or labels[-1] in _SPECIAL_SUFFIXES
                or not all(_LABEL.fullmatch(label) for label in labels)):
            raise PageFetchError("A public Internet hostname is required.") from None
        authority = host
    else:
        if not _public_ip(address):
            raise PageFetchError("The page destination is outside public Internet addresses.")
        authority = f"[{address}]" if address.version == 6 else str(address)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    result = urlunsplit(("https", authority, path, query, ""))
    if len(result) > MAX_URL:
        raise PageFetchError("The encoded page URL is too long.")
    return result


class _PublicConnection(http.client.HTTPSConnection):
    def connect(self):
        # Absolute DNS names avoid local resolver search suffixes. Connect directly
        # to the checked sockaddr and use the URL hostname for TLS and HTTP.
        try:
            ipaddress.ip_address(self.host)
            name = self.host
        except ValueError:
            name = self.host + "."
        records = socket.getaddrinfo(name, self.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        if not records or not all(_public_ip(addr[0]) for _, _, _, _, addr in records):
            raise PageFetchError("The page destination is outside public Internet addresses.")
        for family, kind, protocol, _, addr in records:
            with closing(socket.socket(family, kind, protocol)) as sock:
                sock.settimeout(self.timeout)
                try:
                    sock.connect(addr)
                except OSError:
                    continue
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
        raise PageFetchError("Could not connect to the public page.")


def _response_text(response):
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("text/html", "text/plain"):
        raise PageFetchError("The page reader accepts HTML and plain text.")
    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        raise PageFetchError("The server returned an unsupported content encoding.")
    length = response.headers.get("Content-Length")
    if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 16):
        raise PageFetchError("The server returned an invalid content length.")
    if length is not None and int(length) > MAX_BODY:
        raise PageFetchError("The page exceeds the source byte limit.")
    body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise PageFetchError("The page exceeds the source byte limit.")
    if length is not None and len(body) != int(length):
        raise PageFetchError("The page transfer was incomplete.")
    # Accept ordinary text encodings only. Python's general codec registry also
    # includes transformations unrelated to decoding a page's text.
    try:
        encoding = codecs.lookup(response.headers.get_content_charset() or "utf-8").name
    except (LookupError, ValueError):
        encoding = "utf-8"
    if not re.fullmatch(r"utf-8(?:-sig)?|utf-16(?:-[lb]e)?|ascii|iso8859-[0-9]+|cp125[0-8]|"
                        r"shift_jis|big5|euc_(?:jp|kr)|gb(?:k|2312|18030)|koi8-[ru]", encoding):
        encoding = "utf-8"
    if body.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif body.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        encoding = "utf-16"
    return content_type, body.decode(encoding, errors="replace")


def _fetch(url):
    url = _checked_url(url)
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        path = urlunsplit(("", "", parts.path, parts.query, ""))
        with closing(_PublicConnection(parts.hostname, port=443, timeout=5)) as connection:
            connection.request("GET", path, headers={
                "Accept": "text/html, text/plain", "Accept-Encoding": "identity",
                "User-Agent": "Lantern-Page-Reader/1.0", "Connection": "close",
            })
            with connection.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if (hop == MAX_REDIRECTS or not location or len(location) > MAX_URL
                            or any(char.isspace() or ord(char) < 32 for char in location)):
                        raise PageFetchError("The page returned an invalid or excessive redirect.")
                    url = _checked_url(urljoin(url, location))
                    continue
                if response.status != 200:
                    raise PageFetchError(f"The server returned HTTP {response.status:d}.")
                content_type, text = _response_text(response)
                return FetchedPage(url, content_type, text)


def _fetch_child():
    try:
        result = asdict(_fetch(json.load(sys.stdin)))
    except PageFetchError as exc:
        result = {"error": str(exc)}
    except ssl.SSLCertVerificationError:
        result = {"error": "The server's HTTPS certificate could not be verified."}
    except TimeoutError:
        result = {"error": "The public page timed out."}
    except (ValueError, OSError, http.client.HTTPException):
        result = {"error": "The public page could not be retrieved."}
    print(json.dumps(result, ensure_ascii=True))


def fetch_page(url: str) -> FetchedPage:
    """Fetch one page under a wall-clock deadline, including DNS and TLS."""
    url = _checked_url(url)
    # DNS resolution and trickling HTTP headers have no wall-clock limit in
    # the standard library. A disposable child bounds both without leaking a
    # background request. The package root can be a directory or a zipapp.
    runner = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from lantern.page_fetch import _fetch_child; _fetch_child()")
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ}
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", runner, str(Path(__file__).resolve().parent.parent)],
            input=json.dumps(url).encode("ascii"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=DEADLINE_SECONDS, env=env, check=True,
        )
        result = json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        raise PageFetchError("The public page exceeded the retrieval deadline.") from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise PageFetchError("The public page could not be retrieved.") from None
    if "error" in result:
        raise PageFetchError(result["error"])
    return FetchedPage(**result)
