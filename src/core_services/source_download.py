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
  that is not public (:func:`is_public_host`) at the URL and at every redirect;
* it sends no credentials (a source is any host, and the publisher's token is not for it).

The manifest rule is the one of :func:`src.commands.publisher.publisher.download_from_manifest_url`:
a URL whose path ends in ``.celaut.bee`` is the file; otherwise a UTF-8 text with one
``http(s)`` URL for each line is a manifest of parts, and anything else is the file.

What this does not do: the check of the address is made when the name is resolved, and the
connection resolves it again, so a name that answers with another address in between is not
stopped here. What reaches the registry is still only what hashes to the id that was asked
for (``VALIDATE_ON_IMPORT``, checked by the caller).
"""

import ipaddress
import socket
from contextlib import closing
from typing import Iterator, List, Optional
from urllib.parse import urljoin, urlsplit

import requests

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


def is_public_host(host: str) -> bool:
    """True when every address that ``host`` resolves to is a public one.

    A name that does not resolve is not public. An IPv4 address inside an IPv6 one
    (``::ffff:127.0.0.1``) is judged as the IPv4 address.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    return True


# The check that every request goes through. A test that serves from 127.0.0.1 patches it.
host_is_allowed = is_public_host


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


def _open(url: str, timeout_s: float) -> requests.Response:
    """GET ``url`` and give the response of a 200, with the body still unread.

    The redirects are followed here, one at a time, so that each address is checked.
    """
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SourceDownloadError(f"not an http(s) URL: {url}")
        if not host_is_allowed(parts.hostname):
            raise SourceDownloadError(f"refused: {parts.hostname} is not a public host")
        try:
            response = requests.get(
                url,
                stream=True,
                allow_redirects=False,
                timeout=timeout_s,
                headers={"Accept-Encoding": "identity"},
            )
        except requests.exceptions.RequestException as exc:
            raise SourceDownloadError(f"request failed: {exc}") from exc
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            url = urljoin(url, response.headers["Location"])
            response.close()
            continue
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise SourceDownloadError(f"HTTP {status} from {url}")
        return response
    raise SourceDownloadError(f"more than {MAX_REDIRECTS} redirects")


def _declared_length(response: requests.Response) -> Optional[int]:
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
