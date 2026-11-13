"""Find a first set of peers on the reputation ledgers (issue #494).

Until now a node learned a peer only from ``nodo connect``, from ``IntroducePeer``, from
``Gateway.ResolveNetwork`` or from gossip, and all of them need one peer that the operator
knows. The reputation ledgers hold the signed announcements of nodes that put ERG in a
proof. :func:`discover_peers_from_ledgers` reads them, registers the peers that pass, and
introduces this node to them.

One function does the work, so that the start of the node, a CLI command and a button of
the TUI can all call it. Only the start of the node calls it today
(:func:`startup_discovery`).

* **Ledger agnostic.** The list of peers comes from ``interface._peer_readers``. A second
  ledger is one more entry there.
* **A claim, not a proof.** A peer from a ledger is registered with
  ``manager.add_peer_instance``, as gossip does, so the signature of the announcement and
  the match of key and identity are checked. A ledger peer starts with no local evidence,
  the same as any new peer: ``r̂`` is zero, and ``ô`` applies from the next index refresh.
  It is registered as learned from the network, so the automatic refill does not fund it
  until the operator chooses it.
* **Private addresses.** The signature covers every address, so an address cannot be
  removed from a claim. A claim with an address that is not global (private, loopback,
  link-local, shared CGNAT space, a local name) is refused.
* **A peer that does not answer is removed.** The peer is registered first, because the
  channel and the ``client_id`` need its row. If the introduction then shows that it is
  unreachable (no channel, no ``client_id``, no answer, a timeout), the row that this run
  inserted is removed, like ``nodo disconnect``: an address from a ledger may be old, and
  a dead peer would be tried by the maintenance loop for ever and lose reputation for
  nothing. A peer that answers ``REFUSED`` is alive, so it stays. A failure on this side
  (our own announcement cannot be made) does not say anything about the peer, so it stays
  too.
* **Never fatal.** Every failure is a line in the log. A ledger that cannot be read does
  not stop the others, and a peer that does not answer the introduction does not stop the
  next one. Every read has a deadline.
"""
import ipaddress
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER
from src.utils.peer_gossip import gossip_limit, relayable_peer

DEFAULT_MAX_PEERS = 10
DEFAULT_TIMEOUT_SECONDS = 60
RPC_TIMEOUT_SECONDS = 10

# Names that only mean something inside a network of their own.
_LOCAL_NAME_SUFFIXES = (
    ".local", ".localhost", ".localdomain", ".internal", ".lan", ".home", ".corp",
    ".private", ".home.arpa",
)


@dataclass
class DiscoveryResult:
    """What a run did. ``errors`` maps a ledger tag to why it could not be read."""

    found: int = 0
    added: int = 0
    refused: int = 0
    introduced: int = 0
    refused_introduction: int = 0
    introduction_errors: int = 0
    removed: int = 0
    errors: Dict[str, str] = field(default_factory=dict)


def acceptable_address(host: str) -> bool:
    """Whether ``host`` may be an address of a peer that comes from a ledger.

    An IP literal must be global. A ``.onion`` name is accepted. A DNS name is accepted
    unless it is plainly local (one label, or a suffix of a local network).
    """
    from src.routing import registry as routing

    name = str(host or "").strip().lower().rstrip(".")
    if not name:
        return False
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        if routing.routed_host(name):
            return True
        return "." in name and not name.endswith(_LOCAL_NAME_SUFFIXES)
    return address.is_global and not address.is_multicast


def acceptable_claim(peer, own_key: str) -> bool:
    """Whether a ``Peer`` from a ledger may be given to ``add_peer_instance``.

    Intact and with an address that has not expired (``relayable_peer``), not this node,
    and every address global (see :func:`acceptable_address`).
    """
    if not relayable_peer(peer, getattr(peer, "public_key", "")):
        return False
    if peer.public_key.lower() == own_key:
        return False
    return all(acceptable_address(uri.ip) for uri in peer.uri)


class Introduction(Enum):
    """How the introduction of this node to a peer ended."""

    ACCEPTED = "accepted"
    #: The peer answered and refused: it is alive.
    REFUSED = "refused"
    #: No channel, no ``client_id``, no answer, or a timeout: nobody answered.
    UNREACHABLE = "unreachable"
    #: This node could not make its own announcement. Says nothing about the peer.
    ERROR = "error"


def _introduce_self(peer_id: str) -> Introduction:
    """``IntroducePeer`` to a registered peer, with the announcement of this node.

    Never raises. The steps are the ones of ``connect.connect`` after the handshake.
    """
    from src.gateway.utils import generate_full_node_peer_info
    from src.identity.grpc_transport import peer_channel
    from src.manager.manager import associate_client_id_on_channel, get_client_id_on_other_peer
    from src.utils.bee_client import BeeClient

    try:
        announcement = generate_full_node_peer_info()
    except Exception as e:
        LOGGER(f"[ledger-discovery] cannot make the announcement of this node: {e}")
        return Introduction.ERROR
    try:
        client_id = get_client_id_on_other_peer(peer_id=peer_id)
        if not client_id:
            LOGGER(f"[ledger-discovery] no client_id at {peer_id}: it did not answer.")
            return Introduction.UNREACHABLE
        channel = peer_channel(peer_id=peer_id)
        try:
            result = BeeClient.introduce_peer(
                channel, announcement, client_id=client_id, timeout=RPC_TIMEOUT_SECONDS,
            )
            if result is None:
                LOGGER(f"[ledger-discovery] {peer_id} sent no answer to the introduction.")
                return Introduction.UNREACHABLE
            if result.token == "REFUSED":
                LOGGER(f"[ledger-discovery] {peer_id} refused the introduction.")
                return Introduction.REFUSED
            associate_client_id_on_channel(channel, client_id)
            return Introduction.ACCEPTED
        finally:
            channel.close()
    except Exception as e:
        LOGGER(f"[ledger-discovery] introduction to {peer_id} failed: {e}")
        return Introduction.UNREACHABLE


def discover_peers_from_ledgers(
    limit: Optional[int] = None, timeout: Optional[float] = None,
) -> DiscoveryResult:
    """Register up to ``limit`` well-reputed peers from the ledgers, and introduce this node.

    ``limit`` is ``communication.MAX_PEERS_FROM_LEDGERS`` and ``timeout`` is
    ``communication.LEDGER_DISCOVERY_TIMEOUT_SECONDS`` (seconds, for all the reads
    together) unless given. Does not read ``communication.DISCOVER_PEERS_ON_LEDGERS``:
    that key switches off the automatic run, and a person who asks for a search gets it.
    Never raises.
    """
    result = DiscoveryResult()
    try:
        config = ConfigManager()
        if limit is None:
            limit = gossip_limit(config, "MAX_PEERS_FROM_LEDGERS", DEFAULT_MAX_PEERS)
        if timeout is None:
            try:
                timeout = float(config.get(
                    "communication.LEDGER_DISCOVERY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
                ))
            except (TypeError, ValueError, OverflowError):
                timeout = DEFAULT_TIMEOUT_SECONDS
            if not timeout > 0 or timeout == float("inf"):
                timeout = DEFAULT_TIMEOUT_SECONDS
        if limit <= 0:
            return result
        deadline = time.monotonic() + timeout

        from src.database.sql_connection import SQLConnection
        from src.identity.node_identity import get_node_public_key_hex
        from src.manager.manager import add_peer_instance
        from src.reputation_system.interface import _peer_readers

        sc = SQLConnection()
        known_before = frozenset(sc.get_peers_id())
        own_key = (get_node_public_key_hex() or "").lower()
        introduce = bool(config.get("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", True))

        for ledger, reader in _peer_readers().items():
            remaining = limit - result.added
            if remaining <= 0:
                break
            try:
                claims = reader(remaining, deadline, known_before)
            except Exception as e:
                LOGGER(f"[ledger-discovery] could not read the peers of {ledger}: {e}")
                result.errors[ledger] = str(e)
                continue
            for peer in claims:
                if result.added >= limit:
                    break
                result.found += 1
                try:
                    if not acceptable_claim(peer, own_key):
                        result.refused += 1
                        LOGGER(f"[ledger-discovery] refused {peer.public_key} from {ledger}: "
                               "not intact, expired, this node, or an address that is not global.")
                        continue
                    peer_id = add_peer_instance(peer=peer, learned_via_gossip=True)
                except Exception as e:
                    peer_id = None
                    LOGGER(f"[ledger-discovery] could not register {peer.public_key}: {e}")
                if not peer_id:
                    result.refused += 1
                    continue
                result.added += 1
                LOGGER(f"[ledger-discovery] added {peer_id} from {ledger}.")
                if not introduce:
                    continue
                outcome = _introduce_self(peer_id)
                if outcome is Introduction.ACCEPTED:
                    result.introduced += 1
                elif outcome is Introduction.REFUSED:
                    result.refused_introduction += 1
                elif outcome is Introduction.ERROR:
                    result.introduction_errors += 1
                elif peer_id in known_before:
                    # Not this run's row: the operator or another path put it there.
                    result.introduction_errors += 1
                else:
                    # Nobody answered, so there is no peer at this address now.
                    if sc.remove_peer(peer_id):
                        result.added -= 1
                        result.removed += 1
                        LOGGER(f"[ledger-discovery] removed {peer_id}: it did not answer.")
    except Exception as e:
        LOGGER(f"[ledger-discovery] failed: {e}")
    return result


def startup_discovery() -> None:
    """The run at the start of the node. Never raises, and does nothing when switched off."""
    try:
        if not ConfigManager().get("communication.DISCOVER_PEERS_ON_LEDGERS", True):
            return
        result = discover_peers_from_ledgers()
        LOGGER(
            f"[ledger-discovery] {result.found} found, {result.added} added, "
            f"{result.refused} refused, {result.introduced} introduced, "
            f"{result.refused_introduction} refused the introduction, "
            f"{result.removed} removed (no answer), "
            f"{result.introduction_errors} introduction errors."
        )
    except Exception as e:
        LOGGER(f"[ledger-discovery] startup run failed: {e}")
