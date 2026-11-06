"""A download of a service from the sources that the source-application lists, for the
paths that a caller can trigger (:func:`src.core_services.source_application.acquire_from_sources`
with a limit).

``nodo download`` is the operator's own command and trusts the URL it is given. When a
service or a peer asks for a service by id (``maintain.check_wanted_service``), the URL
comes from a public registry that anybody can write to, so this module does what that
command does not:

* it streams to a file and never holds the service in memory;
* it stops at ``max_bytes`` in all (the ``Content-Length`` of each response first, then
  the bytes actually read, over every part of a manifest);
* it only follows ``http`` and ``https``, and refuses a host that resolves to an address
  that is not public (:func:`is_public_address`) at the URL and at every redirect;
* it reads each URL with one parser (urllib3's ``parse_url``), refuses a URL that another
  parser could read in another way (:func:`parse_source_url`), resolves the host once and
  connects to the address that it checked, so that a name that changes its answer, or a URL
  such as ``http://127.0.0.1:9053\\@example.com/``, cannot send the request elsewhere;
* it sends no credentials (a source is any host, and the publisher's token is not for it),
  and it uses no proxy and no ``.netrc`` from the environment.

The manifest rule is the one of :func:`src.commands.publisher.publisher.download_from_manifest_url`:
a URL whose path ends in ``.celaut.bee`` is the file; otherwise a UTF-8 text with one
``http(s)`` URL for each line is a manifest of parts, and anything else is the file.

What reaches the registry is only what hashes to the id that was asked for
(``VALIDATE_ON_IMPORT``, checked by the caller).
"""

import http.client
import ipaddress
import re
import socket
import ssl
from contextlib import closing
from typing import Iterator, List, NamedTuple, Optional
from urllib.parse import urljoin, urlsplit

from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url

# The redirects that one URL may go through (a GitHub release asset goes through one).
MAX_REDIRECTS = 5
# The most that a manifest, or the start of a file that may be one, is read as text.
MANIFEST_MAX_BYTES = 1 << 20
# The most parts that a manifest may list.
MAX_PARTS = 4096
# What one read of a body asks for.
CHUNK_BYTES = 1 << 20


class SourceDownloadError(Exception):
    """The source cannot be used: refused, too large, or it failed."""


def is_public_address(address: str) -> bool:
    """True when ``address`` (an IP address, as ``getaddrinfo`` gives it) is a public one.

    An IPv4 address inside an IPv6 one (``::ffff:127.0.0.1``) is judged as the IPv4 address.
    """
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def is_public_host(host: str) -> bool:
    """True when every address that ``host`` resolves to is a public one.

    A name that does not resolve is not public.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(is_public_address(address) for address in addresses)


# The check of each address that a request may connect to. A test that serves from
# 127.0.0.1 patches it.
address_is_allowed = is_public_address


class _Budget:
    """The bytes that one download may take; ``limit`` 0 is no limit."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def expect(self, length: int):
        """A response that says how long it is: refuse it before reading any of it."""
        if self.limit and self.used + length > self.limit:
            raise SourceDownloadError(
                f"the source is larger than the limit of {self.limit} bytes "
                f"(it says {length} bytes more, {self.used} were taken)"
            )

    def take(self, length: int):
        self.used += length
        if self.limit and self.used > self.limit:
            raise SourceDownloadError(f"the source is larger than the limit of {self.limit} bytes")


class SourceURL(NamedTuple):
    """Where a request for a URL goes: what :func:`parse_source_url` gives."""

    scheme: str
    host: str  # a name, an IPv4 address, or an IPv6 address without brackets
    port: int
    target: str  # the path and the query, as sent in the request line


# A host as this module accepts it: a name, an IPv4 address, or an IPv6 address in brackets
# (with no zone). Anything else is not a host that a published source needs.
_HOST = re.compile(r"[a-z0-9_]([a-z0-9_.-]*[a-z0-9_])?|\[[0-9a-f:.]+\]")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def parse_source_url(url: str) -> SourceURL:
    """Where a GET of ``url`` goes, or ``SourceDownloadError`` if the URL is refused.

    The URL is read with urllib3's ``parse_url``, and the request goes to what it gives.
    ``urllib.parse`` (which ``urljoin`` uses to follow a redirect) must read the same scheme,
    host and port. A URL that the two parsers could read in two ways is refused before it is
    read: one with a ``\\``, a space, a control character or a character that is not ASCII,
    and one with a user or a password before the host. For example, in
    ``http://127.0.0.1:9053\\@example.com/x`` ``urlsplit`` reads the host ``example.com`` and
    urllib3 the host ``127.0.0.1``.
    """
    if not url.isascii() or "\\" in url or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise SourceDownloadError(f"refused: the URL has a character that is not allowed: {url!r}")
    try:
        parsed = parse_url(url)
        split = urlsplit(url)
        split_port = split.port
    except (LocationParseError, ValueError) as exc:
        raise SourceDownloadError(f"refused: not a valid URL: {url!r}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS or split.scheme.lower() != scheme:
        raise SourceDownloadError(f"not an http(s) URL: {url}")
    if parsed.auth is not None or "@" in split.netloc:
        raise SourceDownloadError(f"refused: a source URL has no user or password: {url!r}")
    host = (parsed.host or "").lower()
    if not _HOST.fullmatch(host) or host.strip("[]") != (split.hostname or ""):
        raise SourceDownloadError(f"refused: the host of the URL is not clear: {url!r}")
    if parsed.port != split_port:
        raise SourceDownloadError(f"refused: the port of the URL is not clear: {url!r}")
    port = parsed.port or _DEFAULT_PORTS[scheme]
    return SourceURL(scheme, host.strip("[]"), port, parsed.request_uri or "/")


def _resolve(host: str, port: int) -> str:
    """The address to connect to for ``host``. Every address of ``host`` must be allowed."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceDownloadError(f"refused: {host} is not a public host ({exc})") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise SourceDownloadError(f"refused: {host} is not a public host (no address)")
    for address in addresses:
        if not address_is_allowed(address):
            raise SourceDownloadError(f"refused: {host} is not a public host ({address})")
    return addresses[0]


_tls_context: Optional[ssl.SSLContext] = None


def _tls() -> ssl.SSLContext:
    """The certificates that ``requests`` uses (certifi), else the ones of the system."""
    global _tls_context
    if _tls_context is None:
        try:
            import certifi

            _tls_context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:  # pragma: no cover - certifi comes with requests
            _tls_context = ssl.create_default_context()
    return _tls_context


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """A connection to ``address``, the address that was checked. ``host`` is what the
    ``Host`` header says; it is not resolved again."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The same over TLS: the certificate and the SNI are for ``host``."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=_tls())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = sock
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _Response:
    """A response of a 200, with its body still unread, and its connection."""

    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse):
        self.connection = connection
        self.response = response
        self.headers = response.headers

    def iter_content(self, size: int) -> Iterator[bytes]:
        try:
            while True:
                chunk = self.response.read1(size)
                if not chunk:
                    return
                yield chunk
        except (OSError, http.client.HTTPException) as exc:
            raise SourceDownloadError(f"the source failed while it was read: {exc}") from exc

    def close(self):
        self.response.close()
        self.connection.close()


_REDIRECTS = (301, 302, 303, 307, 308)
_HEADERS = {"Accept-Encoding": "identity", "User-Agent": "nodo", "Connection": "close"}


def _open(url: str, timeout_s: float) -> _Response:
    """GET ``url`` and give the response of a 200, with the body still unread.

    The redirects are followed here, one at a time, so that each URL is parsed and each
    address is checked in the same way. The connection goes to the address that was
    checked, so the name is not resolved a second time.
    """
    for _ in range(MAX_REDIRECTS + 1):
        source = parse_source_url(url)
        address = _resolve(source.host, source.port)
        kind = _PinnedHTTPSConnection if source.scheme == "https" else _PinnedHTTPConnection
        connection = kind(source.host, source.port, address, timeout_s)
        try:
            connection.request("GET", source.target, headers=_HEADERS)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise SourceDownloadError(f"request failed: {exc}") from exc
        location = response.headers.get("Location")
        if response.status in _REDIRECTS and location:
            response.close()
            connection.close()
            url = urljoin(url, location)
            continue
        if response.status != 200:
            response.close()
            connection.close()
            raise SourceDownloadError(f"HTTP {response.status} from {url}")
        return _Response(connection, response)
    raise SourceDownloadError(f"more than {MAX_REDIRECTS} redirects")


def _declared_length(response: _Response) -> Optional[int]:
    value = response.headers.get("Content-Length", "")
    return int(value) if value.isdigit() else None


def _manifest_urls(head: bytes) -> Optional[List[str]]:
    """The part URLs if ``head`` is a manifest, else ``None``."""
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    urls = [line.strip() for line in text.splitlines() if line.strip()]
    if not urls or not all(u.startswith(("http://", "https://")) for u in urls):
        return None
    return urls


def _is_direct_bee_url(url: str) -> bool:
    return urlsplit(url).path.endswith(".celaut.bee")


def _write(chunks: Iterator[bytes], out, budget: _Budget):
    for chunk in chunks:
        budget.take(len(chunk))
        out.write(chunk)


def download_to_file(url: str, destination: str, max_bytes: int, timeout_s: float = 60) -> int:
    """Download the file (or the parts) that ``url`` names into ``destination``.

    ``max_bytes`` is for all of it (0: no limit). Returns the bytes written. The caller
    owns ``destination``, which may hold a part of the download when this raises.
    """
    budget = _Budget(max_bytes)
    with open(destination, "wb") as out:
        with closing(_open(url, timeout_s)) as response:
            length = _declared_length(response)
            if _is_direct_bee_url(url):
                if length is not None:
                    budget.expect(length)
                _write(response.iter_content(CHUNK_BYTES), out, budget)
                return budget.used

            # Read the start. A manifest is short, so one that is whole in the first
            # MANIFEST_MAX_BYTES is text; a file longer than that is not a manifest.
            chunks = response.iter_content(CHUNK_BYTES)
            head = b""
            for chunk in chunks:
                head += chunk
                if len(head) > MANIFEST_MAX_BYTES:
                    break
            urls = _manifest_urls(head) if len(head) <= MANIFEST_MAX_BYTES else None
            if urls is None:
                if length is not None:
                    budget.expect(length)
                budget.take(len(head))
                out.write(head)
                _write(chunks, out, budget)
                return budget.used

        if len(urls) > MAX_PARTS:
            raise SourceDownloadError(f"the manifest lists more than {MAX_PARTS} parts")
        for part_url in urls:
            with closing(_open(part_url, timeout_s)) as part:
                part_length = _declared_length(part)
                if part_length is not None:
                    budget.expect(part_length)
                _write(part.iter_content(CHUNK_BYTES), out, budget)
    return budget.used
