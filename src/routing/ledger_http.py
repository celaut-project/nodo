"""The requests of the node's own ledger services, direct or through Tor.

The Ergo explorer and node, the Bitcoin explorer and backend, the reputation reads and
the energy meter are chosen by the operator, but the service that answers still sees the
IP address of the host. ``routing.tor.LEDGER_VIA_TOR`` (``auto``: Tor in hidden mode)
sends them through the ``HTTPTunnelPort`` of Tor instead. An address of the operator's
own networks stays direct. When Tor is wanted and is not running, the request fails: it
never falls back to a direct one.

``get`` and ``post`` take what ``requests.get`` and ``requests.post`` take for these
callers, and answer a ``requests``-shaped response. A direct request is a plain
``requests`` call with the same arguments, so nothing changes outside Tor.
"""

import json as _json
from typing import Any, Mapping, Optional
from urllib.parse import urlencode, urlsplit

import requests

from src.routing import tunneled_http
from src.routing.policy import is_local_address, ledger_via_tor

#: A request through Tor has no ``requests`` default to lean on: it needs a number.
DEFAULT_TIMEOUT = 30


class Response:
    """The part of ``requests.Response`` that the ledger clients read."""

    def __init__(self, status_code: int, content: bytes, url: str):
        self.status_code = status_code
        self.content = content
        self.url = url
        self.headers: Mapping[str, str] = {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} for url: {self.url}", response=self
            )


def uses_tor(url: str) -> bool:
    """Whether a request to ``url`` goes through Tor."""
    return ledger_via_tor() and not is_local_address(urlsplit(url).hostname or "")


def _through_tor(method: str, url: str, *, params=None, json=None, data=None,
                 headers=None, timeout=None) -> Response:
    if params:
        url += ("&" if urlsplit(url).query else "?") + urlencode(params)
    sent = dict(headers or {})
    body = data
    if json is not None:
        body = _json.dumps(json)
        sent.setdefault("Content-Type", "application/json")
    try:
        status, content = tunneled_http.request(
            method, url, DEFAULT_TIMEOUT if timeout is None else timeout, body, sent
        )
    except requests.exceptions.RequestException:
        raise
    except Exception as e:
        # Whatever went wrong in the tunnel is a failed request, the one thing the
        # callers already handle; the class tells a reader which.
        raise requests.exceptions.ConnectionError(
            f"{method} {urlsplit(url).netloc} through Tor: {type(e).__name__}"
        ) from None
    return Response(status, content, url)


def get(url: str, **kwargs) -> Any:
    """``requests.get``, through Tor when ``routing.tor.LEDGER_VIA_TOR`` says so."""
    if not uses_tor(url):
        return requests.get(url, **kwargs)
    return _through_tor("GET", url, **kwargs)


def post(url: str, **kwargs) -> Any:
    """``requests.post``, through Tor when ``routing.tor.LEDGER_VIA_TOR`` says so."""
    if not uses_tor(url):
        return requests.post(url, **kwargs)
    return _through_tor("POST", url, **kwargs)
