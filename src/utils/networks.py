"""
Shared-disk / co-location network semantics.

A service can declare Networks in its spec (``celaut.Service.Network``). A
*shared-disk* network is one whose ``protocol_stack`` advertises **virtiofs**:
its instances are meant to share a disk (e.g. one instance writes data, another
with read-only access analyzes it; or Hadoop/Spark-style clusters). In microVMs
(Cloud Hypervisor) disk sharing is done with virtio-fs, which REQUIRES the
participating instances to run on the same physical node (co-location).

A network is identified by its *content*, think ``H(ABCD)``: the sha256 of the
fixed anchor blob (``Service.Network.formal``) that every participating service
writes to disk. Two instances that hold the same anchor data resolve to the same
``network_id``, so they can discover each other and share disk.

Declaring a network in the spec is the capability grant: an instance may ask its
node "give me the instances of network X" and the node obeys ONLY if X is among
the networks declared in that instance's own service spec. The node grants an
instance nothing outside its declared networks.
"""
import hashlib
from typing import Callable, Iterable, List, NamedTuple, Optional, Set

from protos import celaut_pb2 as celaut

# Tags (case-insensitive) that mark a network's protocol stack as virtio-fs
# shared disk. Matched the same way firewall transport tags are matched
# (see src/virtualizers/firewall.py::resolve_slot_transport_protocols).
#
# NOTE: ``Service.Api.Protocol`` is a message (tags/prose/formal), not an enum,
# so virtiofs is expressed as a protocol *tag* convention rather than a new
# proto enum value. No change to the protocol enum is required.
VIRTIOFS_PROTOCOL_TAGS = frozenset({"virtiofs", "virtio-fs", "virtio_fs", "virtiofsd"})

# Read-only shared-disk convention.
#
# A service asks for its shared disk to be mounted *read only* by advertising a
# read-only tag on the same virtiofs network declaration — no proto change is
# needed because ``Service.Network.tags`` / ``Service.Api.Protocol.tags`` are
# free-form repeated strings (the same mechanism virtiofs itself rides on).
#
# Semantics: read-only is a property of *this service's* declaration of the
# network, NOT of the network identity. Two services that share ``H(ABCD)``
# resolve to the SAME network id (see ``network_content_id``), but each side
# independently declares whether it wants the disk rw (writer) or ro (reader).
# So the read-only tag is intentionally excluded from the content id.
READONLY_PROTOCOL_TAGS = frozenset({"readonly", "read-only", "read_only", "ro"})


def _normalized_tags(tags: Iterable[str]) -> List[str]:
    return [str(t).strip().lower() for t in tags if str(t).strip()]


def is_virtiofs_protocol(protocol: celaut.Service.Api.Protocol) -> bool:
    return any(tag in VIRTIOFS_PROTOCOL_TAGS for tag in _normalized_tags(protocol.tags))


def is_virtiofs_network(network: celaut.Service.Network) -> bool:
    """A network is shared-disk when any entry of its protocol_stack is virtiofs."""
    return any(is_virtiofs_protocol(p) for p in network.protocol_stack)


def network_is_readonly(network: celaut.Service.Network) -> bool:
    """
    True when this service declares it wants the shared disk mounted read-only.

    Detected from a read-only tag on the network's own ``tags`` or on any entry
    of its ``protocol_stack`` (case-insensitive; see ``READONLY_PROTOCOL_TAGS``).
    Read-only is per-declaration and does NOT affect ``network_content_id`` — a
    read-only reader and a read-write writer still resolve to the same network.
    """
    if any(tag in READONLY_PROTOCOL_TAGS for tag in _normalized_tags(network.tags)):
        return True
    return any(
        any(tag in READONLY_PROTOCOL_TAGS for tag in _normalized_tags(p.tags))
        for p in network.protocol_stack
    )


def network_content_id(network: celaut.Service.Network) -> bytes:
    """
    Content id of a network, ``H(ABCD)``.

    The identity is the sha256 of the network's fixed anchor blob
    (``Service.Network.formal``). When no anchor is declared we fall back to a
    deterministic hash of the network's identifying fields so that identical
    declarations still collide to the same id.
    """
    if network.formal:
        return hashlib.sha256(network.formal).digest()

    hasher = hashlib.sha256()
    hasher.update(b"celaut-network\x00")
    for tag in sorted(_normalized_tags(network.tags)):
        hasher.update(tag.encode("utf-8"))
        hasher.update(b"\x00")
    hasher.update((network.prose or "").encode("utf-8"))
    hasher.update(b"\x00")
    for protocol in network.protocol_stack:
        for tag in sorted(_normalized_tags(protocol.tags)):
            hasher.update(tag.encode("utf-8"))
            hasher.update(b"\x00")
        hasher.update(protocol.formal or b"")
        hasher.update(b"\x00")
    return hasher.digest()


def declared_network_ids(service: celaut.Service, *, only_virtiofs: bool = False) -> Set[bytes]:
    """Content ids of the networks declared by ``service``."""
    ids: Set[bytes] = set()
    for network in service.network:
        if only_virtiofs and not is_virtiofs_network(network):
            continue
        ids.add(network_content_id(network))
    return ids


def service_declares_network(service: celaut.Service, network_id: bytes) -> bool:
    """Capability check: is ``network_id`` among the networks declared by ``service``?"""
    return bool(network_id) and network_id in declared_network_ids(service)


class DeclaredNetwork(NamedTuple):
    """A summary of one network declared in a service spec."""
    network_id: bytes       # content id H(ABCD)
    network_id_hex: str     # hex form, handy for paths / logs / rpc display
    virtiofs: bool          # shared-disk (virtiofs) network?
    readonly: bool          # does this service want it mounted read-only?
    tags: List[str]         # the network's own declared tags (normalized)


def declared_networks(
    service: celaut.Service, *, only_virtiofs: bool = False
) -> List[DeclaredNetwork]:
    """
    "List my declared networks" — summarize the networks a service declares.

    Deduplicated by content id (a service that declares the same network twice
    yields it once). This is the read-side helper an instance uses to enumerate
    the shared-disk networks it participates in before asking the node for their
    co-located members via ``GetNetworkInstances``.
    """
    out: List[DeclaredNetwork] = []
    seen: Set[bytes] = set()
    for network in service.network:
        virtiofs = is_virtiofs_network(network)
        if only_virtiofs and not virtiofs:
            continue
        nid = network_content_id(network)
        if nid in seen:
            continue
        seen.add(nid)
        out.append(
            DeclaredNetwork(
                network_id=nid,
                network_id_hex=nid.hex(),
                virtiofs=virtiofs,
                readonly=network_is_readonly(network),
                tags=_normalized_tags(network.tags),
            )
        )
    return out


def find_local_network_instances(
    network_id: bytes,
    *,
    local_rows: Iterable[dict],
    load_service: Callable[[str], Optional[celaut.Service]],
) -> List[celaut.Instance]:
    """
    Resolve the co-located instances of ``network_id`` living on this node.

    Args:
        network_id: content id (``H(ABCD)``) of the requested network.
        local_rows: iterable of ``{id, service_id, serialized_instance}`` rows
            (see ``SQLConnection.get_local_instances_with_service``).
        load_service: ``service_id -> Service`` spec loader (e.g.
            ``read_service_from_disk``).
    """
    instances: List[celaut.Instance] = []
    if not network_id:
        return instances

    for row in local_rows:
        service_id = row.get("service_id")
        if not service_id:
            continue
        service = load_service(service_id)
        if service is None:
            continue
        if network_id not in declared_network_ids(service):
            continue
        serialized = row.get("serialized_instance")
        if not serialized:
            continue
        instance = celaut.Instance()
        try:
            instance.ParseFromString(
                serialized if isinstance(serialized, bytes) else serialized.encode("latin-1")
            )
        except Exception:
            continue
        instances.append(instance)
    return instances


def local_node_hosts_network(
    network_id: bytes,
    *,
    local_rows: Iterable[dict],
    load_service: Callable[[str], Optional[celaut.Service]],
) -> bool:
    """True when at least one instance of ``network_id`` already runs on this node."""
    return len(
        find_local_network_instances(
            network_id, local_rows=local_rows, load_service=load_service
        )
    ) > 0


def filter_placements_for_colocation(
    service: celaut.Service,
    peers: dict,
    *,
    local_hosts_network: Callable[[bytes], bool],
    remote_hosts_network: Optional[Callable[[str, bytes], bool]] = None,
    logger_fn: Callable[[str], None] = lambda _msg: None,
) -> dict:
    """
    Restrict candidate placements so that virtiofs shared-disk networks stay
    co-located on a single node.

    ``peers`` maps ``peer_id`` (or the literal ``'local'``) to its EstimatedCost.

    A valid placement is a single node that already hosts *every* virtiofs
    network the service declares (an instance that shares two disks must run
    where BOTH live), or — when nobody hosts them yet — a seed node.

    Policy:
      * If the service declares no virtiofs network, placement is unchanged.
      * If THIS node already hosts all the declared virtiofs networks, the new
        instance MUST run locally to share their disk: every remote peer is
        dropped, leaving only ``'local'``.
      * Else, when ``remote_hosts_network`` is supplied (distributed seeding),
        query peers and, if any peer already hosts all the declared virtiofs
        networks, co-locate the instance THERE — restrict placement to those
        peers and drop everything else. This is how a virtiofs network is seeded
        onto a remote peer that already owns it.
      * Otherwise this is the very first seed of the network. virtio-fs needs a
        same-host shared disk, so the instance is pinned to the local node and
        remote peers are dropped (if the local node has no capacity, no
        co-locating placement exists and an empty set is returned).

    NOTE: *who* launches the first-ever instance of a network when no node hosts
    it yet (cross-node seed election) is deliberately out of scope — we simply
    seed locally, which is always safe.

    ``remote_hosts_network(peer_id, network_id) -> bool`` is best-effort: any
    peer it can't answer for is treated as not hosting the network, so failures
    only ever fall back to the safe local-seed path (never break disk sharing).

    Returns the filtered ``peers`` dict (never mutates the input).
    """
    virtiofs_ids = declared_network_ids(service, only_virtiofs=True)
    if not virtiofs_ids:
        return peers

    # A node is a valid co-location target only if it hosts ALL declared
    # virtiofs networks (their instances must share every one of those disks).
    def _node_hosts_all(probe: Callable[[bytes], bool]) -> bool:
        return all(probe(nid) for nid in virtiofs_ids)

    if _node_hosts_all(local_hosts_network):
        if "local" in peers:
            logger_fn(
                "Virtiofs co-location: network already hosted locally; pinning "
                "instance to the local node and dropping remote peers."
            )
            return {"local": peers["local"]}
        # We host the network locally but have no local execution capacity, and
        # a remote peer cannot share this node's disk. No safe placement.
        logger_fn(
            "Virtiofs co-location: network hosted locally but the local node "
            "has no execution capacity; no co-locating placement is available."
        )
        return {}

    # Distributed seeding: route to a peer that already owns the whole network.
    if remote_hosts_network is not None:
        remote_targets = {
            peer_id: cost
            for peer_id, cost in peers.items()
            if peer_id != "local"
            and all(remote_hosts_network(peer_id, nid) for nid in virtiofs_ids)
        }
        if remote_targets:
            logger_fn(
                "Virtiofs co-location: shared-disk network already hosted by "
                f"peer(s) {sorted(remote_targets)}; routing instance there to "
                "co-locate and dropping non-hosting candidates."
            )
            return remote_targets

    if "local" in peers:
        logger_fn(
            "Virtiofs co-location: seeding shared-disk network locally so "
            "future siblings can co-locate; dropping remote peers."
        )
        return {"local": peers["local"]}

    # No local capacity for a virtiofs service and no peer already hosts it. We
    # refuse to delegate to a peer that cannot be guaranteed to co-locate the
    # whole network. Return no candidates so the launcher surfaces a placement
    # failure rather than silently breaking disk sharing.
    logger_fn(
        "Virtiofs co-location: service declares a shared-disk network but the "
        "local node cannot host it and no peer already owns it; no co-locating "
        f"placement is available (dropped {len(peers)} remote peer candidate(s))."
    )
    return {}
