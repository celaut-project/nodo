"""A download of a service from the sources that the source-application lists, for the
paths that a caller can trigger (:func:`src.core_services.source_application.acquire_from_sources`
with a limit).

``nodo download`` is the operator's own command and trusts the URL it is given. When a
service or a peer asks for a service by id (``maintain.check_wanted_service``), the URL
comes from a public registry that anybody can write to, so this module does what that
command does not:

* it streams to a file and never holds the service in memory;
* with :class:`Limits`, it stops when the time for the service is over, and it gives up a
  source that sends its body more slowly than a minimum (the timeout of a request is for
  each read only, so a source that sends one byte before each timeout never ends without it);
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
import os
import re
import shutil
import socket
import ssl
import threading
import time
from contextlib import closing
from typing import Dict, Iterator, List, NamedTuple, Optional
from urllib.parse import urljoin, urlsplit

from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url

# The redirects that one URL may go through (a GitHub release asset goes through one).
MAX_REDIRECTS = 5
# The most that a manifest, or the start of a file that may be one, is read as text.
MANIFEST_MAX_BYTES = 1 << 20
# The most parts that a manifest may list, unless ``Limits`` says otherwise. ``nodo publish``
# makes parts of PART_SIZE_MB (1000 by default), so a service of 2 GiB has 3.
MAX_PARTS = 64
# What one read of a body asks for.
CHUNK_BYTES = 1 << 20
# The free disk that a download leaves, at least, beyond twice what it may still take (the
# file, and the service that the import extracts from it).
FREE_DISK_RESERVE_BYTES = 64 << 20


class SourceDownloadError(Exception):
    """The source cannot be used: refused, too large, or it failed."""


# IPv6 forms that carry an IPv4 address in their last 32 bits: NAT64 (RFC 6052), which a
# NAT64 network sends to that IPv4 address, and the old IPv4-compatible form.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
# NAT64 for local use (RFC 8215): its IPv4 addresses are not known from the address.
_LOCAL_NAT64 = ipaddress.ip_network("64:ff9b:1::/48")


def is_public_address(address: str) -> bool:
    """True when ``address`` (an IP address, as ``getaddrinfo`` gives it) is a public one.

    An IPv4 address inside an IPv6 one (``::ffff:127.0.0.1``, NAT64 ``64:ff9b::7f00:1``,
    ``::127.0.0.1``, 6to4 ``2002:7f00:1::``) is judged as the IPv4 address. A multicast
    address is not public.
    """
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip in _LOCAL_NAT64:
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or ip in _IPV4_COMPATIBLE:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None and not is_public_address(str(ip.sixtofour)):
            return False
    return ip.is_global and not ip.is_multicast


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
    """The bytes that one download may take; ``limit`` 0 is no limit. ``directory`` is where
    they go: its disk must have room for them (audit N10)."""

    def __init__(self, limit: int, directory: Optional[str] = None):
        self.limit = limit
        self.used = 0
        self.directory = directory

    def expect(self, length: Optional[int]):
        """Before a body is read: refuse it if it says that it is too long, or if the disk has
        no room for it (for what the limit allows still, when it does not say its length)."""
        if length is not None and self.limit and self.used + length > self.limit:
            raise SourceDownloadError(
                f"the source is larger than the limit of {self.limit} bytes "
                f"(it says {length} bytes more, {self.used} were taken)"
            )
        need = length if length is not None else (self.limit - self.used if self.limit else 0)
        if self.directory and need:
            free = shutil.disk_usage(self.directory).free
            if free < 2 * need + FREE_DISK_RESERVE_BYTES:
                raise SourceDownloadError(
                    f"not enough free disk: {need} bytes to download and import, "
                    f"{free} bytes free in {self.directory}"
                )

    def take(self, length: int):
        self.used += length
        if self.limit and self.used > self.limit:
            raise SourceDownloadError(f"the source is larger than the limit of {self.limit} bytes")


class Limits:
    """What the downloads of one service may take together, on the automatic path.

    ``deadline_s``: the most time, in seconds from now, for all of them (every source, every
    part, every redirect). A request that is still open then is cut. 0 is no limit.

    ``min_bytes_per_s``: a body that arrives more slowly than this, on average, after the
    first ``timeout_s`` seconds of it, is given up. 0 is no limit.

    ``max_parts``: the most parts that one manifest may list. ``max_sources``: the most
    sources of the service that are tried. ``max_hosts``: the most hosts that may be asked,
    and ``max_requests_per_host`` the most requests to each one, redirects included (audit N4:
    a manifest of a made-up id made one node send 4096 GETs to a site that is not a source).
    0 is no limit.
    """

    def __init__(self, deadline_s: float = 0, min_bytes_per_s: int = 0, max_parts: int = MAX_PARTS,
                 max_sources: int = 0, max_hosts: int = 0, max_requests_per_host: int = 0):
        self.deadline_s = deadline_s
        self.deadline = time.monotonic() + deadline_s if deadline_s > 0 else None
        self.min_bytes_per_s = min_bytes_per_s
        self.max_parts = max_parts
        self.max_sources = max_sources
        self.max_hosts = max_hosts
        self.max_requests_per_host = max_requests_per_host
        self.requests: Dict[str, int] = {}

    def request(self, host: str):
        """Count a request to ``host``, or refuse it if it is over a limit."""
        if host not in self.requests and self.max_hosts and len(self.requests) >= self.max_hosts:
            raise SourceDownloadError(
                f"refused: the sources of this service ask for more than {self.max_hosts} hosts"
            )
        count = self.requests.get(host, 0) + 1
        if self.max_requests_per_host and count > self.max_requests_per_host:
            raise SourceDownloadError(
                f"refused: more than {self.max_requests_per_host} requests to {host} for this service"
            )
        self.requests[host] = count

    def left(self) -> Optional[float]:
        """The seconds that are left, or ``None`` for no limit. Raises when none are left."""
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise self.over()
        return left

    def over(self) -> "SourceDownloadError":
        return SourceDownloadError(
            f"the download took more than the limit of {self.deadline_s:g} seconds"
        )


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
        self.cut = False

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        _stop_if_cut(self)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The same over TLS: the certificate and the SNI are for ``host``."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=_tls())
        self.address = address
        self.cut = False

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = sock  # so that _Watchdog can cut a slow TLS handshake too
        _stop_if_cut(self)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _stop_if_cut(connection):
    if connection.cut:
        connection.sock.close()
        raise OSError("the time for the download is over")


class _Watchdog:
    """Cuts the socket of ``connection`` after ``seconds`` (``None``: never).

    A timeout of a socket is for one read. A source that sends one byte before each timeout
    (in the headers or in the body) holds a read loop for as long as it wants, so the end of
    the time is put on the socket from another thread: a shutdown wakes the read that waits.
    """

    def __init__(self, connection, seconds: Optional[float]):
        self.connection = connection
        self._timer = None
        if seconds is not None:
            self._timer = threading.Timer(seconds, self._cut)
            self._timer.daemon = True
            self._timer.start()

    @property
    def fired(self) -> bool:
        return self.connection.cut

    def _cut(self):
        self.connection.cut = True
        sock = self.connection.sock
        if sock is not None:
            try:
                # socket.socket's own shutdown, also for an SSLSocket: only the TCP side.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    def cancel(self):
        if self._timer is not None:
            self._timer.cancel()


class _Response:
    """A response of a 200, with its body still unread, and its connection."""

    def __init__(self, connection, response: http.client.HTTPResponse, watchdog: _Watchdog,
                 limits: Limits, timeout_s: float):
        self.connection = connection
        self.response = response
        self.headers = response.headers
        self.watchdog = watchdog
        self.limits = limits
        self.timeout_s = timeout_s

    def iter_content(self, size: int) -> Iterator[bytes]:
        """The body, as it arrives. Raises when the time is over or the body is too slow."""
        start = time.monotonic()
        received = 0
        while True:
            try:
                chunk = self.response.read1(size)
            except (OSError, http.client.HTTPException) as exc:
                if self.watchdog.fired:
                    raise self.limits.over() from exc
                raise SourceDownloadError(f"the source failed while it was read: {exc}") from exc
            if self.watchdog.fired:
                raise self.limits.over()  # what the cut ended is not the whole body
            if not chunk:
                return
            self.limits.left()
            received += len(chunk)
            elapsed = time.monotonic() - start
            minimum = self.limits.min_bytes_per_s
            if minimum and elapsed > self.timeout_s and received < minimum * elapsed:
                raise SourceDownloadError(
                    f"the source is too slow: {received} bytes in {elapsed:.0f} seconds, "
                    f"less than {minimum} bytes each second"
                )
            yield chunk

    def close(self):
        self.watchdog.cancel()
        self.response.close()
        self.connection.close()


_REDIRECTS = (301, 302, 303, 307, 308)
_HEADERS = {"Accept-Encoding": "identity", "User-Agent": "nodo", "Connection": "close"}


def _open(url: str, timeout_s: float, limits: Limits) -> _Response:
    """GET ``url`` and give the response of a 200, with the body still unread.

    The redirects are followed here, one at a time, so that each URL is parsed and each
    address is checked in the same way. The connection goes to the address that was
    checked, so the name is not resolved a second time.
    """
    for _ in range(MAX_REDIRECTS + 1):
        source = parse_source_url(url)
        limits.request(source.host)
        address = _resolve(source.host, source.port)
        left = limits.left()
        kind = _PinnedHTTPSConnection if source.scheme == "https" else _PinnedHTTPConnection
        connection = kind(
            source.host, source.port, address, timeout_s if left is None else min(timeout_s, left)
        )
        watchdog = _Watchdog(connection, left)
        try:
            connection.request("GET", source.target, headers=_HEADERS)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            watchdog.cancel()
            connection.close()
            if watchdog.fired:
                raise limits.over() from exc
            raise SourceDownloadError(f"request failed: {exc}") from exc
        location = response.headers.get("Location")
        if response.status in _REDIRECTS and location:
            watchdog.cancel()
            response.close()
            connection.close()
            url = urljoin(url, location)
            continue
        encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
        if response.status != 200 or encoding != "identity":
            watchdog.cancel()
            response.close()
            connection.close()
            if response.status != 200:
                raise SourceDownloadError(f"HTTP {response.status} from {url}")
            # The request asks for identity. A body in another encoding is not the file, and
            # it is not decoded here: a small gzip body can be a very large file.
            raise SourceDownloadError(f"refused: the source sends Content-Encoding {encoding!r}")
        return _Response(connection, response, watchdog, limits, timeout_s)
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


def download_to_file(url: str, destination: str, max_bytes: int, timeout_s: float = 60,
                     limits: Optional[Limits] = None) -> int:
    """Download the file (or the parts) that ``url`` names into ``destination``.

    ``max_bytes`` is for all of it (0: no limit). ``limits`` is shared by all the downloads of
    one service (none: no limit of time or of pace). Returns the bytes written. The caller
    owns ``destination``, which may hold a part of the download when this raises.
    """
    limits = limits or Limits()
    budget = _Budget(max_bytes, os.path.dirname(os.path.abspath(destination)))
    with open(destination, "wb") as out:
        with closing(_open(url, timeout_s, limits)) as response:
            length = _declared_length(response)
            if _is_direct_bee_url(url):
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
                budget.expect(length)
                budget.take(len(head))
                out.write(head)
                _write(chunks, out, budget)
                return budget.used

        if limits.max_parts and len(urls) > limits.max_parts:
            raise SourceDownloadError(f"the manifest lists more than {limits.max_parts} parts")
        for part_url in urls:
            with closing(_open(part_url, timeout_s, limits)) as part:
                budget.expect(_declared_length(part))
                _write(part.iter_content(CHUNK_BYTES), out, budget)
    return budget.used
