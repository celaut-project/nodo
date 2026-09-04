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
from src.utils.transport_stack import (
    declare_transport_stack,
    share_prose_on_get_peer_info,
)
from src.utils.utils import (
    get_local_ip_from_network,
    is_virtual_interface
)

env_manager = ConfigManager()

# Both read on use rather than at import: neither port may be assigned yet when this
# module loads, and an unassigned TLS one must raise where it is needed, not here.
def _gateway_port() -> int:
    return env_manager.get_gateway_port()


# The plain-gRPC port, when the node serves one (see src/serve.py). Services get this
# one rather than the TLS port: they speak plain gRPC over a hop that never leaves the
# host. 0 when the node serves TLS only.
def _plaintext_gateway_port() -> int:
    return env_manager.get_plaintext_gateway_port()


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
            'every peer will refuse it. Check identity.MNEMONIC.'
        )
        return

    from src.utils.network import uri_expiry

    ts = int(time.time())
    expiry = uri_expiry(ts)
    for uri in peer.uri:
        uri.expiry_unix_timestamp = expiry

    # Both are covered by the signature, so they go on before the digest is taken --
    # signing first and declaring afterwards would produce an announcement whose own
    # signature refuses it.
    #
    # One prose policy for the whole announcement (communication.SHARE_PROSE_ON_*): what
    # a reader is handed to understand the message, as against what a verifier reads to
    # decide about it, and the scheme is no different from the transport stack there.
    declare_signature_scheme(peer, prose=share_prose_on_get_peer_info())

    signature = sign_peer_payload(
        canonical_peer_payload(public_key_hex, ts, canonical_peer_content_digest(peer))
    )
    if not signature:
        # Never leave a declared scheme or an attestation on an unsigned announcement:
        # both are claims the signature is what vouches for, so on their own they state
        # a fact about nothing.
        peer.ClearField("signature_scheme")
        log.LOGGER(
            f'Could not sign this node\'s announcement as {public_key_hex}, so every '
            'peer will refuse it. Check identity.MNEMONIC.'
        )
        return

    peer.public_key = public_key_hex
    peer.signature = signature
    peer.ts = ts


def _build_peer(uris: List[celaut.Instance.Uri]) -> celaut_pb2.Peer:
    peer = celaut_pb2.Peer()

    # Every address this node serves its gateway at. The transport rides on the
    # address itself rather than on a separate slot: tcp:8080 and udp:9000 would be
    # different endpoints, so a reader needs to know which without matching the two
    # by port number.
    for uri in uris:
        announced = peer.uri.add(ip=uri.ip, port=uri.port)
        announced.transport.tags.append("tcp")
        # What the endpoint actually speaks, spelled out rather than named: the tags
        # alone would let two nodes both write "tls" while disagreeing on the extension
        # OID, on what the signature covers or on which RPCs exist, and neither could
        # tell from the announcement. `formal` carries those parameters and is what a
        # comparison reads; the prose is there so a reader can implement the thing (see
        # src/utils/transport_stack.py). Covered by the signature below, so a relay can
        # neither strip the declaration nor edit a parameter out of it.
        declare_transport_stack(announced, prose=share_prose_on_get_peer_info())

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
    ``peer.uri`` is folded into one slot. The rates ride in that slot because an
    ``Instance`` has nowhere else to carry them.

    The port is the *plaintext* one when this node serves it (issue #257): this is what
    a service we execute is told to talk to, and a service speaks plain gRPC. Since the
    service learns the address from this message rather than by convention, there is
    nothing for it to guess -- and nothing to pin. With the plaintext port disabled it
    falls back to the TLS port, and then a service does have to speak TLS.
    """
    port = _plaintext_gateway_port() or _gateway_port()
    instance = celaut.Instance()

    slot = instance.api.slot.add()
    slot.port = port
    slot.transport.CopyFrom(celaut.Service.Api.Protocol(tags=["tcp"]))
    for rate, amount in peer.mu_per_call.items():
        slot.mu_per_call[rate].n = amount.n
    instance.api.payment_contracts.extend(peer.payment_contracts)

    uri_slot = instance.uri_slot.add()
    uri_slot.internal_port = port
    # The peer's addresses, but at the port a service is meant to use: peer.uri carries
    # the announced (TLS) port, which is not the one being handed over here.
    uri_slot.uri.extend(celaut.Instance.Uri(ip=u.ip, port=port) for u in peer.uri)
    return instance


def plaintext_gateway_host() -> str:
    """The single address the plain-gRPC gateway listens on (issue #257).

    Deliberately *not* ``[::]``. That port serves the same, unauthenticated ``Gateway``
    as the TLS one, so every interface it answers on is one more network that reaches
    the full API with nothing to prove who is calling -- which is what the TLS port was
    introduced to stop.

    So it binds where the config file already says this node's gateway is:
    ``virtualizers.ch.NETWORK_BRIDGE_NAME``, the same setting
    :mod:`src.utils.configuration_file` resolves to fill ``__config__.gateway``. The
    listening address and the advertised one come out of one call, so they cannot drift
    apart, and a service finds the port exactly where its ``__config__`` told it to look
    -- the proto contract, rather than a separate env var, decides.

    Loopback is the fallback when the bridge is not up yet (a fresh install, or a
    virtualizer that never created it): the local hop still works, and a caller off-host
    is refused by the kernel rather than by an ACL nobody wrote.
    """
    network = env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "nodo-br-ch")
    try:
        host = _uri_for_network(network).ip
    except Exception as e:
        log.LOGGER(
            f'No address on the gateway network {network} ({e}); binding the plaintext '
            'gateway to loopback. Services on another interface will not reach it until '
            'that network exists.'
        )
        return "127.0.0.1"

    # An interface can hold a wildcard-ish address; refuse it rather than quietly
    # serving the whole host.
    if host in ("0.0.0.0", "::", ""):
        log.LOGGER(
            f'The gateway network {network} resolves to {host!r}, which is every '
            'interface; binding the plaintext gateway to loopback instead.'
        )
        return "127.0.0.1"

    return host


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
