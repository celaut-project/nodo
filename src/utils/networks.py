"""
Shared-disk / co-location network semantics.

A service can declare Networks in its spec (``celaut.Service.Network``). A
*shared-disk* network is one whose ``protocol_stack`` advertises **virtiofs**:
its instances are meant to share a disk (e.g. one instance writes data, another
with read-only access analyzes it; or Hadoop/Spark-style clusters). In microVMs
(Cloud Hypervisor) disk sharing is done with virtio-fs, which REQUIRES the
participating instances to run on the same physical node (co-location).

A shared-disk (virtiofs) network is identified by an explicit **@handle**: a
required identity tag on the network declaration (``Service.Network.tags``)
matching ``^@\\S+$`` — e.g. ``@photos-nate``, ``@llama3-8b``. The id is derived
from that handle, NOT from ``Service.Network.formal``: two services that declare
the same @handle resolve to the SAME ``network_id`` even if their ``formal``
anchor blobs differ, so they discover each other and share disk. ``formal`` is
still carried as the seed/anchor DATA blob written into the shared dir, but it no
longer affects identity. (Non-virtiofs networks keep the legacy content
identity — the sha256 of ``formal`` — and are unaffected by the @handle rule.)

Declaring a network in the spec is the capability grant: an instance may ask its
node "give me the instances of network X" and the node obeys ONLY if X is among
the networks declared in that instance's own service spec. The node grants an
instance nothing outside its declared networks.
"""
import hashlib
import re
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
# network, NOT of the network identity. Two services that share the same
# @handle resolve to the SAME network id (see ``network_content_id``), but each
# side independently declares whether it wants the disk rw (writer) or ro
# (reader). So the read-only tag is intentionally excluded from the identity.
# The read-only tag is a plain free-form tag, distinct from the @handle identity
# tag that lives on the same declaration.
READONLY_PROTOCOL_TAGS = frozenset({"readonly", "read-only", "read_only", "ro"})

# Guest membership convention.
#
# A shared-disk network member is a *guest* when it never wants to CREATE the
# disk — i.e. it must never be the first/seed instance of the network. It may
# only run where instances of that network already exist. A member without this
# tag is a *seed*: it is willing to create the disk and can run anywhere.
#
# Like read-only, this is a free-form tag on the service's own network
# declaration (no proto change) and does NOT affect the network content id.
GUEST_PROTOCOL_TAGS = frozenset({"guest"})

# Identity-handle convention (shared-disk networks only).
#
# A virtiofs shared-disk network's identity is an explicit "@handle" tag on the
# network's own ``tags`` (NOT its ``formal`` anchor). The handle is any tag that,
# after the usual tag normalization, matches ``^@\S+$`` — e.g. ``@photos-nate``.
# This is what Josemi's directive means by "el id debe ser parte de los tags":
# the id must come from the tags, not from H(formal). The handle is a NEW tag
# distinct from the readonly/guest tags that may sit on the same declaration.
IDENTITY_HANDLE_RE = re.compile(r"^@\S+$")

# Domain-separation prefix so the @handle-derived id can't collide with the
# legacy ``H(formal)`` ids used by non-virtiofs networks.
VIRTIOFS_DISK_ID_PREFIX = b"celaut-virtiofs-disk\x00"


def _normalized_tags(tags: Iterable[str]) -> List[str]:
    return [str(t).strip().lower() for t in tags if str(t).strip()]


def _identity_handles(network: celaut.Service.Network) -> List[str]:
    """
    The distinct ``@handle`` identity tags declared on this network's own tags.

    Handles are normalized the same way every other tag is (strip + lowercase,
    see ``_normalized_tags``) so ``@Photos`` and ``@photos`` are the same handle.
    Duplicates collapse; order of first appearance is preserved.
    """
    handles: List[str] = []
    for tag in _normalized_tags(network.tags):
        if IDENTITY_HANDLE_RE.match(tag) and tag not in handles:
            handles.append(tag)
    return handles


def network_identity_handle(network: celaut.Service.Network) -> str:
    """
    The single ``@handle`` that identifies a virtiofs shared-disk network.

    Strict, mirroring Josemi's "must match exactly or it's invalid": a virtiofs
    network MUST declare exactly one distinct ``@handle`` identity tag. Zero
    handles, or more than one distinct handle, is an invalid declaration and
    raises ``ValueError`` (the same way a malformed network declaration is
    rejected rather than silently accepted). The @handle is normalized
    (stripped + lowercased) before it is returned.
    """
    handles = _identity_handles(network)
    if not handles:
        raise ValueError(
            "virtiofs shared-disk network must declare exactly one '@handle' "
            "identity tag (matching '^@\\S+$'); found none"
        )
    if len(handles) > 1:
        raise ValueError(
            "virtiofs shared-disk network must declare exactly one '@handle' "
            f"identity tag; found {len(handles)} distinct: {handles}"
        )
    return handles[0]


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


def network_is_guest(network: celaut.Service.Network) -> bool:
    """
    True when this service joins the shared-disk network only as a *guest* — it
    must never be the first/seed instance and may only run where the network
    already exists. Detected from a ``guest`` tag on the network's own ``tags``
    or any entry of its ``protocol_stack`` (case-insensitive).
    """
    if any(tag in GUEST_PROTOCOL_TAGS for tag in _normalized_tags(network.tags)):
        return True
    return any(
        any(tag in GUEST_PROTOCOL_TAGS for tag in _normalized_tags(p.tags))
        for p in network.protocol_stack
    )


def network_content_id(network: celaut.Service.Network) -> bytes:
    """
    Identity id of a network (32 bytes).

    * **virtiofs shared-disk networks**: the id is derived from the explicit
      ``@handle`` identity tag — ``sha256(VIRTIOFS_DISK_ID_PREFIX + handle)`` —
      NOT from ``formal``. Two declarations with the same @handle but different
      ``formal`` therefore resolve to the SAME id (the whole point). An invalid
      declaration (zero or >1 distinct @handle) raises ``ValueError`` via
      ``network_identity_handle``.
    * **all other networks**: unchanged legacy content id — the sha256 of the
      network's fixed anchor blob (``Service.Network.formal``), falling back to a
      deterministic hash of the network's identifying fields when no anchor is
      declared, so identical declarations still collide to the same id.

    Hashing the @handle (rather than using the raw string as the id) keeps every
    downstream consumer that expects a 32-byte id — paths, the virtio-fs tag,
    sockets, DB keys, capability equality — working unchanged.
    """
    if is_virtiofs_network(network):
        handle = network_identity_handle(network)
        return hashlib.sha256(VIRTIOFS_DISK_ID_PREFIX + handle.encode("utf-8")).digest()

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
    network_id: bytes       # identity id (32 bytes)
    network_id_hex: str     # hex form, handy for paths / logs / rpc display
    virtiofs: bool          # shared-disk (virtiofs) network?
    readonly: bool          # does this service want it mounted read-only?
    guest: bool             # guest-only (never seeds) — runs only where it exists
    tags: List[str]         # the network's own declared tags (normalized)
    disk_handle: str        # the @handle identity of a virtiofs disk ("" if none)


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
                guest=network_is_guest(network),
                tags=_normalized_tags(network.tags),
                disk_handle=network_identity_handle(network) if virtiofs else "",
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


def node_can_host_service(
    service: celaut.Service,
    local_hosts_network: Callable[[bytes], bool],
    *,
    logger_fn: Callable[[str], None] = lambda _msg: None,
) -> bool:
    """
    Can THIS node run ``service`` given its shared-disk (virtiofs) networks?

    This is the co-location admissibility check every node applies to itself when
    asked (locally, or by a peer via the normal ``GetServiceEstimatedCost`` call).
    A node that returns ``False`` simply reports no cost, so the launcher never
    selects it — there is no separate placement negotiation.

    Rules, per declared virtiofs network:
      * **seed** (no ``guest`` tag): the node is willing to CREATE the disk, so it
        can host the service anywhere — hosting is allowed regardless of whether
        the network already exists here.
      * **guest** (``guest`` tag): the service must never be the first instance,
        so the node may host it ONLY if it already hosts that network locally
        (virtio-fs is host-local, so "already exists" means on this node).

    A service with no virtiofs network is always admissible.
    """
    for summary in declared_networks(service, only_virtiofs=True):
        if summary.guest and not local_hosts_network(summary.network_id):
            logger_fn(
                f"Virtiofs: cannot host service — guest-only network "
                f"{summary.disk_handle} ({summary.network_id_hex}) is not present "
                "on this node; a guest instance may only run where the network "
                "already exists."
            )
            return False
    return True
