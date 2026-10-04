"""Re-announce this node's address to known peers as soon as it changes.

Wired into the manager loop as ``network_change_tick()``, same shape as
``energy_tick``/``donations_tick`` (see ``src/manager/energy/monitor.py``,
``src/payment_system/donations/indexer.py``): self-gates to its own interval and
never raises.

The problem this closes: a node that moves networks (a laptop leaving a LAN for a
mobile connection, a dynamic public IP being renewed) keeps being announced by its
old peers at an address it no longer holds. Nothing notices until one of them tries
to reach it, fails, and only *then* re-fetches ``GetPeerInfo`` -- in the meantime
that peer's ``peer_deposits`` tick (``src/manager/maintain.py``) marks it
unreachable and penalises its reputation for an outage that is not one. This tick
detects the change on this node's own side and pushes the new address out with
``IntroducePeer`` before any peer has to notice on its own.

Detection is a plain comparison against the last observed address set, computed with
``_uris_for_all_interfaces()`` (the same helper ``GetPeerInfo``/``IntroducePeer``
already announce from) rather than the signed, priced ``generate_full_node_peer_info()``
-- there is no reason to sign anything or look up payment contracts just to check
whether anything changed. The actual announcement, once a change is confirmed, does
use ``generate_full_node_peer_info()``, exactly like ``nodo connect``'s self-announce
(``src/commands/connect.py``).

Nothing here decides *why* the address changed, and no network event is listened
for -- this is a poll, on the same manager-loop cadence as everything else in
``maintain.py``. A cheap, reliable "did it change" beats a platform-specific
NetworkManager/systemd listener that would need one implementation per OS this node
runs on.
"""

from __future__ import annotations

import time
from typing import FrozenSet, Optional, Tuple

import src.gateway.utils as gateway_utils
from src.database.sql_connection import SQLConnection
from src.gateway.utils import generate_full_node_peer_info
from src.identity.grpc_transport import peer_channel
from src.manager.manager import get_client_id_on_other_peer
from src.utils import logger as log
from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager

LOG_PREFIX = "[network-change]"

DEFAULT_ENABLED = True
DEFAULT_CHECK_INTERVAL_SECONDS = 60

env_manager = ConfigManager()
sc = SQLConnection()

_last_check_monotonic: Optional[float] = None
# None until the first check ever runs (nothing to compare against yet, and every
# known peer already has whatever address this node announced when it was added).
_last_known_addresses: Optional[FrozenSet[Tuple[str, int]]] = None


def _is_enabled() -> bool:
    return bool(env_manager.get("network.REANNOUNCE_ON_NETWORK_CHANGE", DEFAULT_ENABLED))


def _check_interval_seconds() -> float:
    try:
        return float(
            env_manager.get(
                "network.NETWORK_CHANGE_CHECK_INTERVAL_SECONDS",
                DEFAULT_CHECK_INTERVAL_SECONDS,
            )
            or DEFAULT_CHECK_INTERVAL_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_CHECK_INTERVAL_SECONDS


def _current_addresses() -> FrozenSet[Tuple[str, int]]:
    """The ``(ip, port)`` pairs this node would announce right now."""
    return frozenset((uri.ip, uri.port) for uri in gateway_utils._uris_for_all_interfaces())


def _announce_to_known_peers() -> None:
    """Push this node's current address to every peer already on record."""
    peer_ids = sc.get_peers_id()
    if not peer_ids:
        return

    gateway_instance = generate_full_node_peer_info()
    announced = 0
    for peer_id in peer_ids:
        try:
            client_id = get_client_id_on_other_peer(peer_id=peer_id)
        except Exception as exc:
            log.LOGGER(f"{LOG_PREFIX} could not obtain a client_id at peer {peer_id}: {exc}")
            continue

        try:
            channel = peer_channel(peer_id)
        except Exception as exc:
            log.LOGGER(f"{LOG_PREFIX} peer {peer_id} unreachable at its known address, skipping: {exc}")
            continue

        try:
            result = BeeClient.introduce_peer(channel, gateway_instance, client_id=client_id or "")
            if result is None:
                log.LOGGER(f"{LOG_PREFIX} peer {peer_id} sent no answer to IntroducePeer.")
            elif result.token == "REFUSED":
                log.LOGGER(f"{LOG_PREFIX} peer {peer_id} refused this node's announcement.")
            else:
                announced += 1
        except Exception as exc:
            log.LOGGER(f"{LOG_PREFIX} failed to announce the new address to peer {peer_id}: {exc}")
        finally:
            channel.close()

    log.LOGGER(
        f"{LOG_PREFIX} re-announced this node's new address to {announced}/{len(peer_ids)} known peers."
    )


def network_change_tick() -> None:
    """Manager-loop hook. Self-gates to its own interval; never raises."""
    global _last_check_monotonic, _last_known_addresses
    try:
        if not _is_enabled():
            return

        now = time.monotonic()
        last = _last_check_monotonic
        if last is not None and (now - last) < _check_interval_seconds():
            return
        _last_check_monotonic = now

        current = _current_addresses()
        previous = _last_known_addresses
        _last_known_addresses = current

        if previous is None or current == previous:
            return

        log.LOGGER(
            f"{LOG_PREFIX} this node's address changed from {sorted(previous)} to "
            f"{sorted(current)}; re-announcing to known peers."
        )
        _announce_to_known_peers()
    except Exception as exc:
        log.LOGGER(f"{LOG_PREFIX} tick failed: {exc}")
