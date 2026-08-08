import ipaddress
import itertools
import os
import shutil
import threading
import time
from typing import Generator, List, Optional

import netifaces as ni

from src.payment_system.ledgers import local_payment_methods, register_local_contracts
from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.utils import get_local_ip_from_network, get_network_name, to_gas_amount

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")

# Anti-replay sequence counter for this node's signed Peer messages (see
# node_identity.canonical_peer_payload). Reset on restart; that is fine, since
# verifiers key on (ts, seq) together and ts moves forward across restarts too.
_seq_counter = itertools.count(1)


# Guards the lazy recovery below: registering a contract spins up the ledger runtime
# (a JVM and a node connection, for Ergo), so a node that genuinely has no usable
# wallet must not pay that cost on every incoming GetPeerInfo. The lock makes the
# "once per process" bound hold under concurrent GetPeerInfo (gRPC serves it on
# several threads); without it two callers could both retry and spin two JVMs.
_local_contracts_retried = False
_local_contracts_lock = threading.Lock()


def _local_payment_contracts() -> list:
    """The payment contracts to present, recovering from a skipped ledger init.

    ``init()`` writes the LOCAL contract row once at daemon boot and is skipped when
    its runtime dependency is unavailable at that instant — Java installed *after*
    the daemon started, for instance. Presenting ourselves with no payment contract
    makes this node unpayable for as long as it stays up, so retry the registration
    once before announcing nothing.
    """
    global _local_contracts_retried

    contracts = list(local_payment_methods())
    if contracts:
        return contracts

    with _local_contracts_lock:
        # Re-check inside the lock: another thread may have just retried (and
        # possibly registered the contract) while we waited.
        if _local_contracts_retried:
            return list(local_payment_methods())
        _local_contracts_retried = True
        log.LOGGER('No local payment contract registered; retrying the ledger init.')
        register_local_contracts()
        return list(local_payment_methods())


def _uri_for_network(network: str) -> celaut.Instance.Uri:
    uri = celaut.Instance.Uri()
    if network == "localhost":
        uri.ip = "127.0.0.1"
        log.LOGGER('Using localhost IP: 127.0.0.1')
    elif network:
        try:
            uri.ip = ni.ifaddresses(network)[ni.AF_INET][0]['addr']
            log.LOGGER(f'Using network interface {network} with IP: {uri.ip}')
        except (ValueError, KeyError, IndexError) as e:
            log.LOGGER('You must specify a valid interface name ' + network)
            raise Exception('Error generating gateway instance --> ' + str(e))
    else:
        raise ValueError('Network interface name cannot be None')
    uri.port = GATEWAY_PORT
    return uri


def _public_host() -> Optional[str]:
    """The outward-facing host to advertise: ``network.PUBLIC_IP``, else the outbound IP.

    Reuses ``resolve_public_host``, which is exactly the filter issue #236 point 7
    names: it prefers the operator-configured value (a public IP *or* a DNS name --
    which is how a DDNS hostname reaches peers at all), falls back to the outbound
    interface address, and refuses to publish anything private, loopback or
    link-local, since a LAN address is meaningless to a remote peer.
    """
    from src.utils.network import get_local_ip, resolve_public_host

    try:
        outbound_ip = get_local_ip()
    except Exception as e:
        log.LOGGER(f'Could not resolve the outbound IP: {e}')
        outbound_ip = None

    return resolve_public_host(
        configured=str(env_manager.get("network.PUBLIC_IP", "") or ""),
        outbound_ip=outbound_ip,
    )


def _is_loopback(ip: str) -> bool:
    """True for the whole loopback range, not just 127.0.0.1 / ::1."""
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _is_globally_routable(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        # Not an IP literal at all -- a DNS name, which we cannot judge here and
        # which is the operator's explicit choice anyway.
        return True


def _uris_for_all_interfaces() -> List[celaut.Instance.Uri]:
    """Every address this node is reachable at, not just one caller's subnet.

    Before issue #236, GetPeerInfo picked the single interface matching whoever
    asked (``get_network_name(direction=caller_ip)``), so two callers on different
    subnets each learned only the one address matching their own -- never the other.
    ``uri_slot.uri`` is already ``repeated``, so announcing several addresses under
    the one gateway port needs no schema change.

    The public host comes first, because it is the one a remote peer can actually
    use and ``generate_uris_by_peer_id`` yields in insertion order. Private/LAN
    addresses are announced only when ``network.ANNOUNCE_PRIVATE_ADDRESSES`` is set:
    by default they are noise to a remote peer, they cost it a 1s connect timeout
    each, and -- being shared strings like ``172.17.0.1`` -- they collide across
    unrelated peers in the address-keyed lookups.
    """
    uris: List[celaut.Instance.Uri] = []
    seen_ips = set()

    public_host = _public_host()
    if public_host:
        seen_ips.add(public_host)
        uris.append(celaut.Instance.Uri(ip=public_host, port=GATEWAY_PORT))
        log.LOGGER(f'Announcing public host {public_host}:{GATEWAY_PORT}')
    else:
        log.LOGGER('No public address to announce (set network.PUBLIC_IP if behind NAT).')

    announce_private = bool(env_manager.get("network.ANNOUNCE_PRIVATE_ADDRESSES", False))
    private: List[celaut.Instance.Uri] = []
    for interface in ni.interfaces():
        try:
            ip = get_local_ip_from_network(interface, allow_link_local=False)
        except (KeyError, ValueError):
            continue
        if ip in seen_ips or _is_loopback(ip):
            continue
        seen_ips.add(ip)
        if _is_globally_routable(ip):
            uris.append(celaut.Instance.Uri(ip=ip, port=GATEWAY_PORT))
        else:
            private.append(celaut.Instance.Uri(ip=ip, port=GATEWAY_PORT))

    if announce_private:
        uris.extend(private)
    elif not uris:
        # Nothing globally routable to announce -- a node on a LAN with no
        # network.PUBLIC_IP set. Announce the LAN addresses anyway: they are useless
        # to a remote peer, but they are what makes an all-on-one-LAN deployment work
        # at all, and they beat the loopback fallback below, which is useful to nobody.
        log.LOGGER('No routable address; falling back to announcing private addresses.')
        uris.extend(private)

    if not uris:
        # Not even a private address (an isolated box): announce loopback rather than
        # nothing at all, which would make this node unreachable by definition.
        uris = [_uri_for_network(get_network_name(direction="0.0.0.0"))]
    return uris


def _sign_peer(peer: celaut_pb2.Peer) -> None:
    """Sign ``peer`` with this node's identity key, if one is configured yet.

    Issue #236: this signature is what a verifier checks instead of the old
    interactive ``SignPublicKey`` ownership challenge -- GetPeerInfo alone now
    proves the sender controls ``public_key``. A node with no identity mnemonic
    configured (see node_identity.get_identity_mnemonic) is left unsigned, and
    peers fall back to treating it as a legacy, address-identified peer.
    """
    from src.reputation_system.node_identity import (
        canonical_instance_digest,
        canonical_peer_payload,
        get_node_public_key_hex,
        sign_peer_payload,
    )

    public_key_hex = get_node_public_key_hex()
    if not public_key_hex:
        return

    from src.utils.network import announced_address_expiry

    ts = int(time.time())
    seq = next(_seq_counter)
    announced = next(
        (uri.ip for slot in peer.instance.uri_slot for uri in slot.uri), None
    )
    estimated_invalid_after_unix_seconds = announced_address_expiry(announced, ts)
    signature = sign_peer_payload(
        canonical_peer_payload(
            public_key_hex,
            ts,
            seq,
            canonical_instance_digest(peer.instance),
            estimated_invalid_after_unix_seconds,
        )
    )
    if not signature:
        return

    peer.public_key = public_key_hex
    peer.signature = signature
    peer.ts = ts
    peer.seq = seq
    peer.estimated_invalid_after_unix_seconds = estimated_invalid_after_unix_seconds


def _build_peer(uris: List[celaut.Instance.Uri]) -> celaut_pb2.Peer:
    instance = celaut.Instance()

    uri_slot = celaut.Instance.Uri_Slot()
    uri_slot.internal_port = GATEWAY_PORT
    uri_slot.uri.extend(uris)
    instance.uri_slot.append(uri_slot)

    slot = celaut.Service.Api.Slot()
    slot.port = GATEWAY_PORT
    slot.transport.CopyFrom(celaut.Service.Api.Protocol(tags=["tcp"]))

    # Advertise what this node charges on a recurring basis, so a peer knows the
    # rate before negotiating anything. The price of a *specific service* is not
    # here: that is what GetServiceEstimatedCost is for. Values are ceilings; see
    # node_advertised_rates(). This rides in the gateway slot because a peer
    # already stores that slot verbatim (manager.add_peer_instance keeps
    # api.slot[0] in peer.protocol_stack), so it needs no schema of its own.
    #
    # Imported here, like local_proofs below: the cost-function package reaches the
    # virtualizer stack, which imports this module back at import time.
    from src.utils.cost_functions.general_cost_functions import node_advertised_rates

    for rate, gas in node_advertised_rates().items():
        slot.gas_amount_per_call[rate].n = str(gas)

    instance.api.slot.append(slot)

    payment_contracts = _local_payment_contracts()
    log.LOGGER(f'Using {len(payment_contracts)} local payment methods')
    if payment_contracts:
        instance.api.payment_contracts.extend(payment_contracts)

    from src.reputation_system.fetch import local_proofs

    reputation_proofs = list(local_proofs())
    log.LOGGER(f'Using {len(reputation_proofs)} local reputation proofs')

    peer = celaut_pb2.Peer(
        reputation_proofs=reputation_proofs,
        instance=instance
    )
    _sign_peer(peer)
    return peer


def generate_node_peer_info(network: str) -> celaut_pb2.Peer:
    """A Peer advertising a single, specific network's address.

    For internal, non-discovery uses that need exactly one network's address (a
    container's bridge network, a service's own gateway config) -- P2P discovery
    uses :func:`generate_full_node_peer_info` instead.
    """
    log.LOGGER(f'Generating gateway instance for the network {network}')
    return _build_peer([_uri_for_network(network)])


def generate_full_node_peer_info() -> celaut_pb2.Peer:
    """A Peer advertising every address this node is reachable at, signed with its
    identity key. Used for peer-to-peer discovery (GetPeerInfo, IntroducePeer
    self-announce) -- see :func:`generate_node_peer_info` for the single-network form.
    """
    log.LOGGER('Generating gateway instance for all reachable interfaces')
    return _build_peer(_uris_for_all_interfaces())


# If the service is not on the registry, save it.
def save_service(
        metadata: Optional[celaut.Metadata],
        service_dir: str,
        service_hash: str
) -> bool:
    def __save():
        log.LOGGER('Save service on disk')
        try:
            shutil.move(service_dir, REGISTRY + service_hash)
            return True
        except Exception as e:
            log.LOGGER(f'Exception saving a service {service_hash}: ' + str(e))
            return False
        finally:
            if metadata:
                try:
                    with open(METADATA_REGISTRY + service_hash, "wb") as f:
                        f.write(metadata.SerializeToString())
                except Exception as e:
                    log.LOGGER(f'Exception writing metadata of {service_hash}: ' + str(e))

    return os.path.exists(os.path.join(METADATA_REGISTRY, service_hash)) or __save()
