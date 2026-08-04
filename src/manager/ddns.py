"""Dynamic DNS — keep a hostname pointing at this node's public IP.

Service tunneling means only the node's gateway port has to be reachable, but it
still has to *be* reachable, and a home connection's public IP changes. This
publishes the current one to a DDNS provider on a schedule, so peers can find the
node by name instead of by an address that expires.

Reaching the node from outside also needs the router to forward the gateway port;
that is the operator's job, and ``nodo info`` reports whether it looks done.

Who calls this
--------------
``ddns_tick()`` runs from ``manager_thread`` (``src/manager/maintain.py``) on every
iteration and self-gates to ``ddns.INTERVAL_SECONDS``, following the same pattern
as the low-demand scheduler: calling it more often than its own cadence is a cheap
no-op, and it never raises, so the manager loop is never disturbed by it.

Which IP gets published
-----------------------
By default **none is sent**, and the provider records the source address of the
request. That is deliberate: behind NAT the node cannot see its own public
address, and the packet's source is the only thing that is definitely right.
``network.PUBLIC_IP`` overrides that when an operator knows better (a static
address, or a node behind a proxy whose egress differs from its ingress).

Providers
---------
deSEC (``update.dedyn.io``) is the default and the only one implemented. It speaks
the classic dyndns2 update protocol, answering ``good`` or ``nochg``. Adding
another provider means another entry in ``_PROVIDERS``.
"""

import socket
import time
from typing import Callable, Dict, Optional, Tuple

import requests

from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()

DESEC = "desec"
DESEC_UPDATE_URL = "https://update.dedyn.io/"

DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_TIMEOUT_SECONDS = 15

# dyndns2 success bodies: the record was set, or it already held this value.
_DYNDNS_OK = ("good", "nochg")

LOG_PREFIX = "[DDNS]"

# Set by ddns_tick(); the cadence gate lives here so the tick stays a plain call.
_last_tick_monotonic: Optional[float] = None


class DdnsError(Exception):
    """An update could not be completed. Always caught before reaching the caller."""


def _setting(key: str, default=None):
    return env_manager.get(f"ddns.{key}", default)


def is_enabled() -> bool:
    return bool(_setting("ENABLED", False))


def interval_seconds() -> int:
    try:
        configured = int(_setting("INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
        return configured if configured > 0 else DEFAULT_INTERVAL_SECONDS
    except (TypeError, ValueError):
        log.LOGGER(
            f"{LOG_PREFIX} ddns.INTERVAL_SECONDS is not a number; "
            f"using {DEFAULT_INTERVAL_SECONDS}s."
        )
        return DEFAULT_INTERVAL_SECONDS


def configured_hostname() -> str:
    return str(_setting("DOMAIN", "") or "").strip()


def _token() -> str:
    return str(_setting("TOKEN", "") or "").strip()


def configured_public_ip() -> Optional[str]:
    """The address an operator pinned, or None to let the provider use the source IP."""
    pinned = str(env_manager.get("network.PUBLIC_IP", "") or "").strip()
    return pinned or None


def _update_desec(hostname: str, token: str, ip: Optional[str]) -> str:
    """Send one dyndns2 update to deSEC. Returns the provider's answer."""
    params: Dict[str, str] = {"hostname": hostname}
    if ip:
        # deSEC picks the field by family; only IPv4 is published for now.
        params["myipv4"] = ip

    try:
        response = requests.get(
            DESEC_UPDATE_URL,
            params=params,
            headers={"Authorization": f"Token {token}"},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise DdnsError(f"cannot reach deSEC: {e}")

    body = (response.text or "").strip().lower()

    if response.status_code == 401:
        raise DdnsError("deSEC rejected the token (401).")
    if response.status_code == 404:
        raise DdnsError(f"deSEC does not know the hostname '{hostname}' (404).")
    if response.status_code != 200:
        raise DdnsError(f"deSEC answered {response.status_code}: {body or 'no body'}")
    if not body.startswith(_DYNDNS_OK):
        raise DdnsError(f"deSEC answered '{body}'")

    return body


_PROVIDERS: Dict[str, Callable[[str, str, Optional[str]], str]] = {
    DESEC: _update_desec,
}


def _provider() -> Tuple[str, Callable[[str, str, Optional[str]], str]]:
    name = str(_setting("PROVIDER", DESEC) or DESEC).strip().lower()
    if name in _PROVIDERS:
        return name, _PROVIDERS[name]

    log.LOGGER(
        f"{LOG_PREFIX} ddns.PROVIDER='{name}' is not implemented "
        f"(available: {sorted(_PROVIDERS)}); using '{DESEC}'."
    )
    return DESEC, _PROVIDERS[DESEC]


def resolved_ip(hostname: str) -> Optional[str]:
    """What the hostname currently resolves to, for confirming an update landed."""
    try:
        return socket.getaddrinfo(hostname, None, socket.AF_INET)[0][4][0]
    except (socket.gaierror, IndexError):
        return None


def publish_public_ip() -> bool:
    """Publish this node's public IP once. Returns whether the provider accepted it.

    Raises ``DdnsError`` with the reason when it does not, so a caller that wants
    to report the failure (a command, a status check) can; ``ddns_tick`` logs it.
    """
    hostname = configured_hostname()
    if not hostname:
        raise DdnsError("ddns.DOMAIN is not set.")

    token = _token()
    if not token:
        raise DdnsError("ddns.TOKEN is not set.")

    provider_name, update = _provider()
    ip = configured_public_ip()

    answer = update(hostname, token, ip)

    published = ip or resolved_ip(hostname)
    log.LOGGER(
        f"{LOG_PREFIX} {provider_name} accepted '{answer}' for {hostname}"
        + (f" -> {published}" if published else " (source address)")
    )
    return True


def status() -> Dict[str, Optional[str]]:
    """What a status view needs: config and live resolution.

    There is no "last published IP" here on purpose: that state would live in
    whichever process last called ``publish_public_ip()`` (normally the daemon),
    and a CLI command like ``nodo info`` runs in a *different* process that would
    always see it empty. ``resolves_to`` below is the only honest signal a fresh
    process has.
    """
    hostname = configured_hostname()
    return {
        "enabled": is_enabled(),
        "provider": str(_setting("PROVIDER", DESEC) or DESEC),
        "hostname": hostname or None,
        "configured_ip": configured_public_ip(),
        "resolves_to": resolved_ip(hostname) if hostname else None,
        "interval_seconds": interval_seconds(),
    }


def ddns_tick() -> None:
    """One manager-loop iteration of the DDNS updater. Never raises.

    Self-gates to ``ddns.INTERVAL_SECONDS``, so calling it every manager iteration
    (which ticks far faster) is a cheap no-op in between.
    """
    global _last_tick_monotonic
    try:
        if not is_enabled():
            return

        now = time.monotonic()
        if _last_tick_monotonic is not None and (now - _last_tick_monotonic) < interval_seconds():
            return
        _last_tick_monotonic = now

        publish_public_ip()

    except DdnsError as e:
        log.LOGGER(f"{LOG_PREFIX} update failed: {e}")
    except Exception as e:
        # The manager loop must survive anything this does.
        log.LOGGER(f"{LOG_PREFIX} unexpected failure: {type(e).__name__}: {e}")
