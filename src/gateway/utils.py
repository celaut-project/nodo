import ipaddress
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
from src.utils.utils import (
    get_local_ip_from_network,
    is_virtual_interface
)

env_manager = ConfigManager()

# Read on use rather than at import: the port may not be assigned yet when this
# module loads, and an unassigned one must raise where it is needed, not here.
def _gateway_port() -> int:
    return env_manager.get_gateway_port()


REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")


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
    uri.port = _gateway_port()
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

    Container/VPN interfaces (docker0, br-*, veth*, ...) are skipped outright via
    ``is_virtual_interface``: their addresses (e.g. 172.17.0.1) are real on this
    host but unreachable from anywhere else, so they are never a candidate LAN
    address, let alone a routable one.

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
        gateway_port = _gateway_port()
        uris.append(celaut.Instance.Uri(ip=public_host, port=gateway_port))
        log.LOGGER(f'Announcing public host {public_host}:{gateway_port}')
    else:
        log.LOGGER('No public address to announce (set network.PUBLIC_IP if behind NAT).')

    announce_private = bool(env_manager.get("network.ANNOUNCE_PRIVATE_ADDRESSES", False))
    private: List[celaut.Instance.Uri] = []
    for interface in ni.interfaces():
        if is_virtual_interface(interface):
            continue
        try:
            ip = get_local_ip_from_network(interface, allow_link_local=False)
        except (KeyError, ValueError):
            continue
        if ip in seen_ips or _is_loopback(ip):
            continue
        seen_ips.add(ip)
        if _is_globally_routable(ip):
            uris.append(celaut.Instance.Uri(ip=ip, port=_gateway_port()))
        else:
            private.append(celaut.Instance.Uri(ip=ip, port=_gateway_port()))

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
        # Not even a private address. Loopback is only useful to this process and is
        # actively wrong for any remote caller, so announce nothing instead.
        log.LOGGER('No reachable address to announce; leaving GetPeerInfo uri list empty.')
    return uris


def _sign_peer(peer: celaut_pb2.Peer) -> None:
    """Sign ``peer`` with this node's identity key.

    Issue #236: this signature is what a verifier checks instead of the old
    interactive ``SignPublicKey`` ownership challenge -- GetPeerInfo alone now
    proves the sender controls ``public_key``. There is no fallback behind it: a peer
    refuses an announcement it cannot verify (``manager.add_peer_instance``), so an
    unsigned node is invisible to the network rather than address-identified.

    Both ways out without a signature should be unreachable -- every node derives an
    identity key from the mnemonic ConfigManager generates on first load -- so each one
    logs. The refusal itself is only ever logged by the *remote* peer, so staying quiet
    here would leave an unreachable node with nothing locally to explain why.
    """
    from src.reputation_system.node_identity import (
        canonical_peer_content_digest,
        canonical_peer_payload,
        declare_signature_scheme,
        get_node_public_key_hex,
        sign_peer_payload,
    )

    public_key_hex = get_node_public_key_hex()
    if not public_key_hex:
        log.LOGGER(
            'No identity public key, so this node is announcing itself UNSIGNED and '
            'every peer will refuse it. Check ledgers.ergo.WALLET_MNEMONIC.'
        )
        return

    from src.utils.network import uri_expiry

    ts = int(time.time())
    expiry = uri_expiry(ts)
    for uri in peer.uri:
        uri.expiry_unix_timestamp = expiry
    signature = sign_peer_payload(
        canonical_peer_payload(public_key_hex, ts, canonical_peer_content_digest(peer))
    )
    if not signature:
        log.LOGGER(
            f'Could not sign this node\'s announcement as {public_key_hex}, so every '
            'peer will refuse it. Check ledgers.ergo.WALLET_MNEMONIC.'
        )
        return

    peer.public_key = public_key_hex
    peer.signature = signature
    peer.ts = ts
    # Declared alongside the signature, never without one: the field says what this
    # signature is, so on an unsigned announcement it would state a fact about nothing.
    declare_signature_scheme(peer)


def _build_peer(uris: List[celaut.Instance.Uri]) -> celaut_pb2.Peer:
    peer = celaut_pb2.Peer()

    # Every address this node serves its gateway at. The transport rides on the
    # address itself rather than on a separate slot: tcp:8080 and udp:9000 would be
    # different endpoints, so a reader needs to know which without matching the two
    # by port number.
    for uri in uris:
        announced = peer.uri.add(ip=uri.ip, port=uri.port)
        announced.transport.tags.append("tcp")

    # Advertise what this node charges on a recurring basis, so a peer knows the
    # rate before negotiating anything. The price of a *specific service* is not
    # here: that is what GetServiceEstimatedCost is for. Values are ceilings; see
    # node_advertised_rates(). Node-wide rather than per-address, because a node's
    # rates do not depend on which of its addresses you reach it through.
    #
    # Imported here, like local_proofs below: the cost-function package reaches the
    # virtualizer stack, which imports this module back at import time.
    from src.utils.cost_functions.general_cost_functions import node_advertised_rates

    for rate, amount_mu in node_advertised_rates().items():
        peer.mu_per_call[rate].n = str(amount_mu)

    payment_contracts = _local_payment_contracts()
    log.LOGGER(f'Using {len(payment_contracts)} local payment methods')
    if payment_contracts:
        peer.payment_contracts.extend(payment_contracts)

    from src.reputation_system.fetch import local_proofs

    reputation_proofs = list(local_proofs())
    log.LOGGER(f'Using {len(reputation_proofs)} local reputation proofs')
    peer.reputation_proofs.extend(reputation_proofs)

    _sign_peer(peer)
    return peer


def peer_gateway_instance(peer: celaut_pb2.Peer) -> celaut.Instance:
    """Convert a ``Peer`` into the ``Instance`` shape ``ConfigurationFile.gateway``
    expects.

    ``Instance`` still groups addresses under an ``internal_port``, so all of
    ``peer.uri`` is folded into one slot at ``GATEWAY_PORT`` -- the only port a
    self-generated ``Peer`` ever serves. The rates ride in that slot because an
    ``Instance`` has nowhere else to carry them.
    """
    instance = celaut.Instance()

    slot = instance.api.slot.add()
    slot.port = _gateway_port()
    slot.transport.CopyFrom(celaut.Service.Api.Protocol(tags=["tcp"]))
    for rate, amount in peer.mu_per_call.items():
        slot.mu_per_call[rate].n = amount.n
    instance.api.payment_contracts.extend(peer.payment_contracts)

    uri_slot = instance.uri_slot.add()
    uri_slot.internal_port = _gateway_port()
    uri_slot.uri.extend(celaut.Instance.Uri(ip=u.ip, port=u.port) for u in peer.uri)
    return instance


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
        except Exception as e:
            log.LOGGER(f'Exception saving a service {service_hash}: ' + str(e))
            return False
        if metadata:
            try:
                with open(METADATA_REGISTRY + service_hash, "wb") as f:
                    f.write(metadata.SerializeToString())
            except Exception as e:
                log.LOGGER(f'Exception writing metadata of {service_hash}: ' + str(e))
                return False
        return True

    return os.path.exists(os.path.join(REGISTRY, service_hash)) or __save()
