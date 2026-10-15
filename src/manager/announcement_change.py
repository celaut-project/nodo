"""Re-announce this node to known peers whenever its announcement changes.

Wired into the manager loop as ``announcement_change_tick()``, same shape as
``energy_tick``/``donations_tick`` (see ``src/manager/energy/monitor.py``,
``src/payment_system/donations/indexer.py``): self-gates to its own interval and
never raises.

A peer learns what this node announces -- the signed ``Peer`` from
``generate_full_node_peer_info()`` -- when they connect, and keeps it
(``peer.advertisement``). Anything that changes that message afterwards is a claim the
peer goes on believing until something makes it re-fetch: an address the node no
longer holds (a laptop leaving a LAN, a renewed dynamic public IP), for which the
peer's ``peer_deposits`` tick (``src/manager/maintain.py``) penalises it as
unreachable; resources it no longer offers (``network.EXECUTE_LOCALLY: false``
announces none), which keep being summed into every peer's upper bound and keep
attracting quotes; rates, payment contracts, reputation proofs or benchmark scores
that moved. This tick notices the change on this node's own side and pushes the new
``Peer`` to every known peer with ``IntroducePeer``, which a peer stores over the old
one (``manager.add_peer_instance``: a newer ``ts`` from a known peer is an update).

Detection compares ``gateway.utils.announcement_digest()`` -- the content digest the
signed-announcement cache keys on, taken without signing -- against the digest last
pushed. That one is kept on disk (``main.CACHE``), not in memory, because the commonest
way the announcement changes is a configuration edit, and every edit from the TUI
restarts the node: an in-memory "last seen" would be gone by the time the change could
be compared against it. No record at all (a fresh cache, or the first boot of a node
that predates this) counts as a change, so peers holding an announcement from before
are told once.

A round is recorded once it has been attempted, whether or not every peer took it. A
peer that missed it keeps the old announcement until it re-fetches ``GetPeerInfo`` on
its own, as it always has; retrying unreachable peers every interval would cost a
connection timeout each, every minute, for peers that are mostly simply gone.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import src.gateway.utils as gateway_utils
from src.database.sql_connection import SQLConnection
from src.gateway.utils import generate_full_node_peer_info
from src.identity.grpc_transport import peer_channel
from src.manager.manager import get_client_id_on_other_peer
from src.utils import logger as log
from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager

LOG_PREFIX = "[announcement]"

DEFAULT_ENABLED = True
DEFAULT_CHECK_INTERVAL_SECONDS = 60
DIGEST_FILE = "announced_peer_digest"

env_manager = ConfigManager()
sc = SQLConnection()

_last_check_monotonic: Optional[float] = None


def _setting(key: str, former_key: str, default):
    """``network.<key>``, or the key it was called while this only tracked addresses."""
    value = env_manager.get(f"network.{key}", None)
    if value is None:
        value = env_manager.get(f"network.{former_key}", None)
    return default if value is None else value


def _is_enabled() -> bool:
    return bool(_setting("REANNOUNCE_ON_CHANGE", "REANNOUNCE_ON_NETWORK_CHANGE", DEFAULT_ENABLED))


def _check_interval_seconds() -> float:
    try:
        return float(
            _setting(
                "ANNOUNCEMENT_CHECK_INTERVAL_SECONDS",
                "NETWORK_CHANGE_CHECK_INTERVAL_SECONDS",
                DEFAULT_CHECK_INTERVAL_SECONDS,
            )
            or DEFAULT_CHECK_INTERVAL_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_CHECK_INTERVAL_SECONDS


def _digest_path() -> str:
    cache = str(env_manager.get("CACHE", "") or "").strip()
    if not cache or "${" in cache:
        cache = os.path.dirname(os.path.realpath(env_manager.config_path)) or "."
    return os.path.join(cache, DIGEST_FILE)


def _last_announced_digest() -> Optional[str]:
    try:
        with open(_digest_path(), "r") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _record_announced_digest(digest: str) -> None:
    path = _digest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(digest)
    os.replace(tmp, path)


def _announce_to_known_peers() -> None:
    """Push this node's current announcement to every peer already on record."""
    peer_ids = sc.get_peers_id()
    if not peer_ids:
        return

    # Freshly signed: a cached signature could carry a ts older than the announcement
    # these peers already hold, and they would drop it as stale.
    gateway_instance = generate_full_node_peer_info(fresh=True)
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
            log.LOGGER(f"{LOG_PREFIX} failed to send the new announcement to peer {peer_id}: {exc}")
        finally:
            channel.close()

    log.LOGGER(
        f"{LOG_PREFIX} re-announced this node to {announced}/{len(peer_ids)} known peers."
    )


def announcement_change_tick() -> None:
    """Manager-loop hook. Self-gates to its own interval; never raises."""
    global _last_check_monotonic
    try:
        if not _is_enabled():
            return

        now = time.monotonic()
        last = _last_check_monotonic
        if last is not None and (now - last) < _check_interval_seconds():
            return
        _last_check_monotonic = now

        current = gateway_utils.announcement_digest()
        previous = _last_announced_digest()
        if current == previous:
            return

        log.LOGGER(
            f"{LOG_PREFIX} this node's announcement changed "
            f"({previous[:12] if previous else 'none recorded'} -> {current[:12]}); "
            "re-announcing to known peers."
        )
        _announce_to_known_peers()
        _record_announced_digest(current)
    except Exception as exc:
        log.LOGGER(f"{LOG_PREFIX} tick failed: {exc}")
