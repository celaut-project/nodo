"""A plain HTTP GET through Tor, for the requests the host makes for its services.

Tor's ``HTTPTunnelPort`` speaks only ``CONNECT``, so an ``http://`` URL cannot go through
it as an ordinary HTTP proxy request. ``http.client`` opens the tunnel with ``CONNECT``
whatever the scheme (``set_tunnel``), and runs TLS inside it for ``https://``, with the
name of the target for SNI and for the certificate check.
"""

import http.client
import ssl
from typing import Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

from src.routing.base import RoutingUnavailable

# A chain node's REST answers are small; a peer that sends more is not one to read.
MAX_BODY_BYTES = 4 * 1024 * 1024


def get(url: str, timeout: float) -> Tuple[int, bytes]:
    """``(status, body)`` of ``GET url``, through Tor. Raises on any transport failure."""
    return request("GET", url, timeout, headers={"Accept": "application/json"})


def request(
    method: str,
    url: str,
    timeout: float,
    body: Union[bytes, str, None] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> Tuple[int, bytes]:
    """``(status, body)`` of one request, through Tor. Raises on any transport failure."""
    from src.routing import registry

    proxy_port = registry.TOR.http_tunnel_port()
    if proxy_port is None:
        raise RoutingUnavailable(f"{url} is reached through Tor, and Tor is not running.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Not an http(s) URL: {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            "127.0.0.1", proxy_port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=timeout)
    try:
        connection.set_tunnel(parts.hostname, port)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request(
            method, path,
            body=body.encode("utf-8") if isinstance(body, str) else body,
            headers=dict(headers or {}),
        )
        response = connection.getresponse()
        return response.status, response.read(MAX_BODY_BYTES)
    finally:
        connection.close()
