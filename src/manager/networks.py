import socket, os, json
from typing import Dict, List, Optional, Tuple
from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager
from src.utils.registry_errors import ServiceNotInRegistry, ServiceSpecUnavailable
from src.utils.utils import load_service_from_disk
from src.identity.node_identity import (
    ComponentFormalError,
    parse_component_formal,
    same_component,
    same_component_stack,
)
from src.manager.network_templates import find_placeholders
from src.utils.logger import LOGGER
from src.utils.network_policy import enforce_network_policy
from src.manager.network_defaults import configured_endpoints, endpoint_addresses
from src.manager.pow_networks import POW_TAG_PREFIX, resolve_pow_network
from src.virtualizers.firewall import allow_all_egress, allow_connection_to_instance

env_manager = ConfigManager()
sc = SQLConnection()

class NetworkAuthorizationError(Exception):
    """The networks an instance may use could not be derived from its ancestry.

    Raised instead of granting: the launch is aborted rather than continued with a
    set of networks nobody checked.
    """


class NetworkRequestRejected(Exception):
    """A ``Gateway.ResolveNetwork`` request asked for more than the caller declared.

    Distinct from :class:`NetworkAuthorizationError` (this node could not work out
    what the caller is allowed) and from ``NetworkPolicyRejection`` (the operator
    refuses the domain to anyone): here the node knows exactly who is asking and what
    they declared, and the request does not fit inside it. The three are separate
    because a caller can act on the difference -- fix the request, retry later, or
    stop asking this node.
    """

# What a hostname tag opens when its entry names no port: the pair `resolve_domain` has
# always opened, and as much as a name on its own can say (#389).
DEFAULT_HOSTNAME_PORTS = (80, 443)


def hostname_ports(network: celaut.Service.Network) -> List[int]:
    """The ports a hostname tag is opened on, from the network entry's ``formal``.

    **A declaration does not choose a peer's port.** Everything else this module
    resolves is an instance running on some node, and that node assigned the port when
    it published it -- whatever the instance would have preferred. That is why an
    ``Instance.Uri`` carries a port and a ``Service.Network`` does not: local members
    are offered on the ports their launcher recorded, the operator's seeds on the ports
    the operator wrote down, and a ``pow:`` peer on the P2P port somebody observed it
    on.

    A hostname is the one exception, because there is no instance and no node that
    published one -- only a name this node looks up. The port has nowhere else to come
    from, and what the entry states is the *standard* port the service answering to
    that name is expected to be on: ``port=<n>`` in the entry's ``formal``, the same
    ``key=value`` body every other parameter of an entry is written in, ``${VAR}``
    selection included (#385) since it is substituted before this runs.

    An entry that states no port opens :data:`DEFAULT_HOSTNAME_PORTS`, which is what a
    bare hostname tag has always meant.

    A ``port`` this node cannot honour -- not a port number, in a ``formal`` that is not
    readable -- yields **nothing**, and the caller grants nothing for the tag. Falling
    back to 80 and 443 there would open two ports nobody asked for on the strength of a
    declaration this node could not read: the hole ``docs/NETWORKS.md`` already refuses
    for a ``pow:`` peer's second uri, at once useless for the ask and open. The packer
    refuses that shape (``_validate_network_ports``), so only a pack made before it did
    reaches here.
    """
    if not network.formal:
        return list(DEFAULT_HOSTNAME_PORTS)

    try:
        value = parse_component_formal(network.formal).get("port")
    except ComponentFormalError as e:
        LOGGER(f"[NETWORKS] a network entry's formal is not readable ({e}).")
        return []

    if value is None:
        return list(DEFAULT_HOSTNAME_PORTS)
    if not (value.isdigit() and 0 < int(value) < 65536):
        LOGGER(
            f"[NETWORKS] a network entry declares port={value!r}, which is not a port "
            "number."
        )
        return []
    return [int(value)]


def resolve_domain(domain: str, ports=DEFAULT_HOSTNAME_PORTS) -> List[celaut.Instance.Uri]:
    """A hostname's IPv4 addresses, one ``Uri`` per (address, port).

    What this grants is **addresses**, and only that. The guest is not given a way to
    look the name up: nodo serves no DNS and opens no port 53 (see the note in
    ``virtualizers/microvm/network.py``), so a program inside the guest that takes a
    URL -- and therefore calls ``getaddrinfo`` -- fails before it ever uses the allow
    written here. These addresses are usable by a program that reads them out of
    ``__config__`` and connects to them directly (#389).

    ``ports`` is what :func:`hostname_ports` read out of the network entry, defaulting
    to what a bare hostname tag has always opened.
    """
    try:
        ips = list({
            info[4][0]
            for info in socket.getaddrinfo(domain, None)
            if info[0] == socket.AF_INET
        })
        return [
            celaut.Instance.Uri(ip=ip, port=port)
            for ip in ips
            for port in ports
        ]
    except socket.gaierror:
        raise ValueError(f"Cannot resolve domain: {domain}")

# Which ledger tags resolve to no peer instances at all, and why. A payment system is
# not a service this node can be pointed at: `bitcoin` is reached over its own node's
# JSON-RPC, configured under `ledgers.bitcoin`, and there is no URI to advertise for it.
# Named here rather than left to fall through the loop below, so the answer is a
# statement instead of an accident.
LEDGERS_WITHOUT_URIS = ("bitcoin",)


def resolve_ergo_network() -> List[celaut.Instance.Uri]:
    return []

    # TODO Needs to get the ip and port from the data, actually is the restApiUrl.
    try:
        http_peers_file = env_manager.get("ledgers.ergo.HTTP_PEERS_PATH")
        if os.path.exists(http_peers_file):         
            with open(http_peers_file, 'r') as f:
                ergo_peers = json.load(f)

            result=[]
            for uri in ergo_peers.keys():
                ip, port = uri.split(":")
                result.append(celaut.Instance.Uri(ip=ip, port=int(port)))
            
            return result
    except:
        return []

def _slot_exposes(slot: celaut.Service.Api.Slot, network: celaut.Service.Network) -> bool:
    """Whether one API slot offers what peers of ``network`` are expected to speak.

    ``same_component_stack`` is the comparison every other protocol stack on this node
    gets (a peer's transport stack, a signature scheme), so it is the one used here
    too: the slot has to pair up one-to-one with the network's ``protocol_stack``,
    formal deciding where both sides carry one and a shared tag otherwise.

    A network that states no ``protocol_stack`` asks nothing of the slot but that it
    exist: the requirement is then only that the instance *can* be consumed, which any
    slot satisfies. Comparing against an empty stack literally would instead require
    a slot that declares nothing, which no service that means to be reached writes.
    """
    if not len(network.protocol_stack):
        return True
    return same_component_stack(slot.protocol_stack, network.protocol_stack)


def local_network_instances(
    network: celaut.Service.Network,
    requester_id: Optional[str] = None,
) -> List[Tuple[str, celaut.Instance]]:
    """The instances this node runs that are members of ``network`` (#387).

    The rule is the one ``docs/NETWORKS.md`` ("Network Instance Indexing") has
    stated since before it was implemented: an instance is indexed as a member of a
    network only if **both**

    1. its service declares that network in ``Service.network`` -- it *wants* peers
       there (``match_networks``: formal when both carry one, a shared tag otherwise),
       and
    2. its service exposes, in some ``Service.Api`` slot, the ``protocol_stack`` the
       network expects of its peers -- it *can* be consumed there. An instance that
       only declares the network is a consumer of it, not a node of it, and is not
       offered to anyone.

    What is returned per member is not its stored ``Instance`` verbatim but a view of
    it narrowed to the slots that satisfied condition 2, with the ``uri_slot`` entries
    for those ports. The consumer of a resolution writes a firewall rule per URI it is
    handed (``configure_guest_firewall_policy``), so an instance that also exposes an
    unrelated admin port is not opened on that port to a guest that asked for the
    network on another. A member with no advertisable address on any qualifying slot
    (the launcher recorded none -- the caller was expected to tunnel) is skipped, since
    there is nothing to hand out.

    ``requester_id`` names the instance the resolution is for, when it is one this
    node runs, and it is never listed as its own peer. At launch the instance is not on
    the registry yet, so nothing needs excluding; over ``Gateway.ResolveNetwork`` it is,
    and would otherwise satisfy both conditions by construction.

    An instance whose spec or definition cannot be read right now is skipped with a
    log line, not raised over. This is a source of *candidates*, like the operator's
    seeds: nothing is granted by naming one, and the launch that is waiting for the
    answer is not made to fail because an unrelated instance was mid-teardown.

    Pairs of ``(instance_id, Instance)`` rather than bare Instances, matching the
    other member sources this module builds.
    """
    members: List[Tuple[str, celaut.Instance]] = []
    for instance_id in sc.get_all_internal_containers_ids():
        if requester_id and instance_id == requester_id:
            continue
        try:
            service_id = sc.get_service_id_by_container_id(id=instance_id)
            spec = load_service_from_disk(service_hash=service_id)
        except Exception as e:
            LOGGER(
                f"[NETWORKS] skipping local instance {instance_id} as a network member: "
                f"its spec could not be read ({type(e).__name__}: {e})."
            )
            continue

        if not any(match_networks(network, declared) for declared in spec.network):
            continue

        exposing = [slot for slot in spec.api.slot if _slot_exposes(slot, network)]
        if not exposing:
            continue

        serialized = sc.get_internal_instance(id=instance_id)
        if not serialized:
            # Registered but its definition not yet stored: the guest is still
            # booting and its published addresses are not known.
            continue
        stored = celaut.Instance()
        try:
            stored.ParseFromString(serialized)
        except Exception as e:
            LOGGER(
                f"[NETWORKS] skipping local instance {instance_id} as a network member: "
                f"its stored definition is unreadable ({e})."
            )
            continue

        ports = {slot.port for slot in exposing}
        uri_slots = [
            uri_slot for uri_slot in stored.uri_slot
            if uri_slot.internal_port in ports and len(uri_slot.uri)
        ]
        if not uri_slots:
            continue

        member = celaut.Instance(
            api=celaut.Service.Api(
                slot=exposing,
                payment_contracts=stored.api.payment_contracts,
            ),
            uri_slot=uri_slots,
        )
        members.append((instance_id, member))
    return members


def _local_peers(
    network: celaut.Service.Network,
    requester_id: Optional[str],
) -> List[celaut.Instance]:
    """Local members of ``network``, as bare Instances."""
    return [
        instance for _, instance in
        local_network_instances(network, requester_id=requester_id)
    ]


def resolve_network(
    network: celaut.Service.Network,
    ask_peers: bool = True,
    requester_id: Optional[str] = None,
) -> List[celaut.Instance]:
    """Peer instances for one declared communication domain.

    **The tags of an entry are synonyms**, so the walk below stops at the first one
    that resolves rather than accumulating what every tag yields. They are alternative
    names for the single destination the entry declares -- the reading
    ``docs/CONCEPTS.md`` gives a scheme component's tags, and the one that makes
    :func:`match_networks` right to conclude identity from a single shared tag. A
    service wanting two destinations declares two entries; an entry answered under one
    of its names has been answered. Stopping early is the semantics, not an
    optimization.

    ``ask_peers=False`` forbids the resolution from asking other celaut nodes, and is
    passed by ``Gateway.ResolveNetwork`` when answering one. It is what keeps a question
    from being relayed: see :func:`pow_networks.candidate_urls`.

    ``requester_id`` is the local instance the answer is for, if it is one, so that it
    is not offered itself as a peer (:func:`local_network_instances`).

    Sources, for a tag that is not a ``pow:`` domain: **the instances this node runs**
    that are members of the network (#387), and **the operator's seeds** for the tag
    (``service_networks.default_instances``), and failing the latter, a DNS lookup of
    a tag shaped like a hostname. The local members are always included alongside
    whichever of the other two answered: a guest that declares ``postgres`` should
    reach both the ``postgres`` this node runs and the one the operator wrote down,
    and neither source knows about the other.

    Local members are **not** offered for a ``pow:`` network. Membership there is
    decided by verifying each candidate's chain state, not by what its spec declares,
    and ``resolve_pow_network`` owns that verification; a local instance that wants
    to be found for one enters through the same candidate list as everyone else.
    """
    # Wildcard "*" (open-internet egress) and any unresolved tag resolve to no
    # concrete peer instances; initialise uris so an unmatched loop cannot raise
    # UnboundLocalError (it previously did for tag "*").
    uris: List[celaut.Instance.Uri] = []
    is_pow = any(tag.startswith(POW_TAG_PREFIX) for tag in network.tags)
    local = [] if is_pow else _local_peers(network, requester_id)
    for tag in network.tags:
        # A `pow:<chain>` tag names a PoW communication domain, whose actual ask
        # lives in `Network.formal` (issue #78,
        # docs/proposals/78-network-guarantees-and-pow.md). It is dispatched first
        # and returns whole Instances rather than bare uris, because which peers
        # qualify is decided by verifying each candidate's chain state -- not by a
        # name lookup. The prefix carries no `.`, so such a tag could never have
        # reached the DNS heuristic below anyway; the order is for clarity.
        if tag.startswith(POW_TAG_PREFIX):
            peers = resolve_pow_network(network, tag=tag, ask_peers=ask_peers)
            if peers:
                return peers
            continue

        # Operator seeds apply to any tag; PoW above still verifies its candidates.
        addresses = endpoint_addresses(configured_endpoints(tag, config=env_manager))
        if addresses:
            peers = [celaut.Instance(
                api=celaut.Service.Api(slot=[celaut.Service.Api.Slot(
                    port=1, transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                    protocol_stack=network.protocol_stack)]),
                uri_slot=[celaut.Instance.Uri_Slot(internal_port=1,
                    uri=[celaut.Instance.Uri(ip=ip, port=port)])],
            ) for ip, port in addresses]
            return local + peers

        if tag in LEDGERS_WITHOUT_URIS:
            continue

        if "ergo" in tag:
            uris = resolve_ergo_network()
            if uris:
                break

        if not tag.islower() or '.' not in tag:
            continue

        ports = hostname_ports(network)
        if not ports:
            # The entry names a host and a port this node cannot read (`hostname_ports`
            # logged which). Granting the historical 80 and 443 instead would open what
            # nobody declared, so the posture is the one taken just below for a name
            # that does not resolve: the tag yields no peers and the guest boots
            # with default-deny toward it.
            LOGGER(
                f"[NETWORKS] tag {tag!r} names a host, but its entry declares a port "
                "this node cannot read; no peers granted for it. "
                "The guest boots without an allow toward it."
            )
            continue

        try:
            uris = resolve_domain(tag, ports=ports)
        except ValueError as e:
            # A name that does not resolve is not a reason to fail the launch (#391).
            # It used to be: the error escaped to the launcher's catch-all, which
            # tore the VM down and reported `Cannot resolve domain: <tag>` -- for a
            # typo, a host that is down right now, or a `*.cdn.example` glob the
            # packer now refuses but older packs still carry. The posture taken is
            # the one `build_network_resolution` already takes for a deferred
            # template: the tag yields no peers, the guest boots with default-deny
            # toward it, and the reason is on the log. Naming a glob as what it is,
            # because "cannot resolve" is a true statement that points away from
            # the cause.
            shape = (
                "is a wildcard hostname, which the resolver does not support"
                if tag.startswith("*") else "did not resolve"
            )
            LOGGER(
                f"[NETWORKS] tag {tag!r} {shape} ({e}); no peers granted for it. "
                "The guest boots without an allow toward it."
            )
            uris = []
        if uris:
            break

    if not uris:
        # No concrete peer URIs (e.g. wildcard "*"): the tag is honoured at the
        # firewall layer (allow-all egress); there is no external peer instance to
        # advertise. Local members, if any, are still the answer.
        return local

    client_protocol_stack = network.protocol_stack
    i_slot = 1  # Default slot id (because internal port usage is irrelevant here)

    instance = celaut.Instance(
        api=celaut.Service.Api(
            slot=[celaut.Service.Api.Slot(
                port=i_slot,
                transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                protocol_stack=client_protocol_stack
            )],
            payment_contracts=[]
        ),
        uri_slot=[celaut.Instance.Uri_Slot(
            internal_port = i_slot,
            uri=uris
        )]
    )

    return local + [instance]

def _formal_pairs(formal: bytes, whose: str) -> Dict[str, str]:
    """``formal`` as pairs for the subset check, or a rejection saying whose is bad.

    Unlike the template layer's tolerant reader, this one raises: the whole check is
    a comparison of two documents, and a document that cannot be read is not one that
    agreed with the other. ``whose`` names which side failed, because the caller can
    fix its own request and can do nothing about the declaration on this node.
    """
    try:
        return parse_component_formal(formal)
    except ComponentFormalError as e:
        raise NetworkRequestRejected(f"{whose} formal is not a key=value body: {e}") from None


def request_fits_declaration(
    declared: celaut.Service.Network,
    requested: celaut.Service.Network,
) -> Optional[str]:
    """``None`` if ``requested`` fits inside ``declared``, else why it does not (#385).

    The rule a ``Gateway.ResolveNetwork`` request is held to, stated once so it can be
    read and tested as a rule rather than inferred from a handler. A request fits when:

    * **the tags are the same set.** A tag is what the operator's ``service_networks``
      policy is written against and what dispatches the resolution, so a request that
      renames the domain is asking about a different domain -- not a narrowing of this
      one. Set equality, not intersection: ``match_networks`` may accept one shared
      tag between two *declarations* (they are loose by design), but this is not two
      declarations, it is a caller proposing a completion of its own.
    * **every key the declaration fixed is present in the request with exactly that
      value.** These are the identity keys plus whatever selection the author already
      made. A request that drops one broadens the ask -- ``pow.block_id`` removed
      turns "contains block B" into "any peer" -- and a request that alters one asks
      for a domain the service never declared. Both are refused, and dropping is
      refused as firmly as altering precisely because it is the one that *looks*
      harmless.
    * **keys the declaration left templated may be filled with anything**, including
      another template (a caller narrowing in two steps), and
    * **the request may add keys the declaration never mentioned.** Adding is always a
      narrowing: every reader of a ``formal`` either enforces a key or carries it, so
      a key the author did not write can only cost the caller peers, never gain it
      any. This is what makes "the instantiator picks" expressible at all.

    The asymmetry is the point. Templates and additions narrow; removals and edits
    broaden; only the narrowing direction is granted.
    """
    if set(declared.tags) != set(requested.tags):
        return (
            f"tags {sorted(requested.tags)} are not the declared {sorted(declared.tags)}. "
            "A ResolveNetwork request completes a domain the caller declared; it does "
            "not name a different one."
        )

    declared_pairs = _formal_pairs(declared.formal, "the declared network's")
    requested_pairs = _formal_pairs(requested.formal, "the requested network's")
    open_keys = set(find_placeholders(declared.formal))

    for key, value in declared_pairs.items():
        if key in open_keys:
            continue
        if key not in requested_pairs:
            return (
                f"the request drops {key!r}, which the service declared as {value!r}. "
                "Dropping a fixed key broadens the ask; only keys the declaration "
                "left as ${VAR} may be filled, and new keys may be added."
            )
        if requested_pairs[key] != value:
            return (
                f"the request changes {key!r} from {value!r} to "
                f"{requested_pairs[key]!r}. That key is fixed by the service's own "
                "declaration; a caller may fill a ${VAR} or add a key, not rewrite "
                "what the author decided."
            )

    return None


def check_network_request(
    declared_networks: List[celaut.Service.Network],
    requested: celaut.Service.Network,
) -> None:
    """Raise :class:`NetworkRequestRejected` unless ``requested`` fits some declaration.

    Some, not all: a service declares several networks and is asking about one of
    them, so the request is accepted if it fits **any** of them. The rejection then
    reports every declaration it was measured against and why each one refused it,
    because a verdict against one of a set says nothing useful on its own -- the same
    reasoning the network policy's rejection report is built on.

    A caller that declared no network at all is refused outright rather than granted
    the generous reading: an empty declaration is "I asked for no domain", and
    resolving one for it would make the whole check optional for anybody willing to
    declare nothing.
    """
    if not declared_networks:
        raise NetworkRequestRejected(
            "the calling instance's service declares no network, so there is nothing "
            f"for a request about {sorted(requested.tags)} to fit inside."
        )

    reasons = []
    for index, declared in enumerate(declared_networks, start=1):
        reason = request_fits_declaration(declared, requested)
        if reason is None:
            return
        reasons.append(f"  #{index} {sorted(declared.tags)}: {reason}")

    raise NetworkRequestRejected(
        f"the request for {sorted(requested.tags)} does not fit any network the "
        "calling instance's service declares:\n" + "\n".join(reasons)
    )


def declared_networks_of_caller(caller_ip: str) -> Optional[List[celaut.Service.Network]]:
    """The networks declared by the local instance at ``caller_ip``, or ``None``.

    ``None`` means **"this node cannot tell who is asking"**, and it is not the same
    answer as an empty list. A ``ResolveNetwork`` caller is very often not a local
    instance at all -- it is another celaut node, asking as a peer, over the same RPC
    (``pow_networks._peer_suggested_endpoints`` makes exactly that call) -- and such a
    caller has no spec on this node to be measured against. The subset check does not
    apply to it, and inventing an empty declaration for it would turn the check into a
    blanket refusal of peer-to-peer resolution.

    The identification is the one ``ModifyServiceSystemResources`` already uses: the
    gRPC peer's address against ``local_instances.ip``. It is reused rather than
    re-derived so there is one answer on this node to "which instance is this", and a
    guest that cannot be identified for one RPC is not identified for the other.

    A spec that is on the registry but unreadable right now returns ``None`` too, and
    that is a deliberate difference from ``filter_networks_with_ancestors``, which
    aborts on the same condition. It aborts because it is deciding what to *open* for
    a guest that is about to run, where "cannot tell" must not read as "allowed". Here
    nothing is opened: the answer is a list of addresses the caller verifies itself,
    so falling back to the pre-#385 behaviour costs a caller-side check nobody was
    doing a week ago, and failing closed on a transient memory-lock timeout would
    break resolution for an instance that is behaving perfectly.
    """
    if not caller_ip:
        return None
    container_id = sc.get_local_instance_id_by_uri(uri=caller_ip)
    if not container_id:
        return None
    try:
        # `get_service_id_by_container_id` raises a bare Exception for "no row",
        # which is why this catches broadly rather than naming the registry errors:
        # every way of not arriving at a spec has the same consequence here, and the
        # consequence is the pre-#385 behaviour, not a refusal.
        service_id = sc.get_service_id_by_container_id(id=container_id)
        spec = load_service_from_disk(service_hash=service_id)
    except Exception as e:
        LOGGER(
            f"[NETWORKS] ResolveNetwork: {caller_ip} is local instance {container_id} "
            f"but its spec could not be read ({type(e).__name__}: {e}); the request is "
            "not checked against a declaration."
        )
        return None
    return list(spec.network)


def grant_resolved_network(
    vmachine_id: str,
    resolution: celaut.ConfigurationFile.NetworkResolution,
) -> None:
    """Open firewall rules for a network a local instance just completed (#404).

    ``configure_guest_firewall_policy`` writes a VM's firewall once, at launch, from
    whatever ``build_network_resolution`` could resolve *then*. A network deferred
    past launch -- an unfilled ``${VAR}`` (#385) -- is answered later over
    ``Gateway.ResolveNetwork``, but until now that answer was informational only:
    the guest learned the right addresses and stayed default-deny toward every one
    of them, because nothing ever wrote a rule for a resolution that arrived after
    boot. This is the missing half, called from :func:`resolve_network_for_peer`
    once a request is confirmed to fit the calling instance's own declaration --
    the same ``allow_connection_to_instance``/``allow_all_egress`` primitives launch
    itself uses, invoked here instead, additive rather than replacing anything.
    ``block_all`` is deliberately not called again: that is launch's job, run once
    per VM, and this only adds to the deny-by-default policy already standing for
    ``vmachine_id``.

    Best-effort, like the launch-time policy it mirrors: a rule that fails to apply
    is logged, not raised, because this runs after the RPC has already committed to
    answering the guest, and there is no retry the guest could usefully perform.
    """
    tag = resolution.tags[0] if resolution.tags else "<untagged>"

    if any(t == "*" for t in resolution.tags):
        if not allow_all_egress(vmachine_id=vmachine_id):
            LOGGER(
                f"[NETWORKS] ResolveNetwork grant for {vmachine_id}: failed to allow "
                f"all egress for network tag {tag!r}."
            )
        return

    applied = 0
    for instance in resolution.peer_instances:
        if allow_connection_to_instance(vmachine_id=vmachine_id, instance=instance):
            applied += 1

    if applied:
        LOGGER(
            f"[NETWORKS] ResolveNetwork grant for {vmachine_id}: opened firewall for "
            f"network tag {tag!r}, {applied} of {len(resolution.peer_instances)} peer "
            "instance(s)."
        )
    elif resolution.peer_instances:
        LOGGER(
            f"[NETWORKS] ResolveNetwork grant for {vmachine_id}: no firewall rule "
            f"could be applied for network tag {tag!r}."
        )


def resolve_network_for_peer(
    network: celaut.Service.Network,
    subject: str = "",
    caller_ip: str = "",
) -> celaut.ConfigurationFile.NetworkResolution:
    """Answer another node's ``Gateway.ResolveNetwork`` question about ``network``.

    The decisions behind that RPC, kept out of its gRPC plumbing so they can be read
    and tested as decisions. Two of them:

    * **The operator's policy applies.** ``service_networks`` says which domains this
      node will reach on anyone's behalf. Resolving one it refuses to reach -- handing
      over the addresses it would not use itself -- is reaching it by proxy, the same
      argument that puts the check before the balancer in ``launch_service`` rather
      than after it. The rejection propagates as a rejection, so the caller can tell
      "not from this node" from "nobody is there".
    * **The question is never relayed** (``ask_peers=False``). Answering it by asking
      our own peers, who ask theirs, is a walk over a graph nobody has a view of -- and
      two nodes that know each other are already a cycle. Each node answers from what
      it knows, and a caller wanting more breadth asks more nodes itself, which keeps
      the cost with whoever chose to spend it.

    A third, added by #385: **when the caller is a local instance, the request has to
    fit what its own service declared** (:func:`check_network_request`). Before this,
    the RPC resolved any ``Service.Network`` handed to it, so a guest allowed to reach
    ``pow:ergo`` with block B pinned could ask for ``pow:ergo`` with no block at all
    and be answered with peers on any Ergo-shaped chain -- the deferred-resolution
    path would otherwise have been a way around the declaration it exists to complete.
    The check applies only when ``caller_ip`` identifies a local instance; a remote
    node asking as a peer has no spec here and keeps the behaviour it had.

    Note what the check does **not** do: it does not grant. It only refuses requests
    that do not fit; the operator policy above still decides whether this node reaches
    the domain at all, and it runs first, so a caller learns "not from this node"
    before it learns anything about its own declaration.
    """
    enforce_network_policy(networks=[network], subject=subject)

    # An unfilled `${VAR}` is not a question this node can answer: there is no peer
    # holding a block named after a variable. Refused here, and before the caller is
    # identified, because it is a property of the request itself and the answer is
    # the same for a local guest and a remote node. Deliberately *not* substituted
    # from anything on this node either: the whole point of a template is that the
    # value comes from whoever instantiated the service, and this node filling one in
    # from its own environment would be picking the chain on their behalf.
    open_keys = find_placeholders(network.formal)
    if open_keys:
        raise NetworkRequestRejected(
            f"the requested formal still carries unfilled templates: "
            f"{', '.join(sorted(open_keys))}. A ResolveNetwork request has to be the "
            "completed ask -- this node does not fill a ${VAR} in on a caller's "
            "behalf, because which instance of the protocol is meant is the caller's "
            "decision to make."
        )

    declared = declared_networks_of_caller(caller_ip)
    if declared is not None:
        check_network_request(declared_networks=declared, requested=network)

    # A local caller is on the registry by now and, declaring the network and quite
    # possibly exposing its protocols, would qualify as its own peer (#387).
    requester_id = sc.get_local_instance_id_by_uri(uri=caller_ip) if caller_ip else None

    resolution = celaut.ConfigurationFile.NetworkResolution(
        tags=list(network.tags),
        peer_instances=resolve_network(
            network,
            ask_peers=False,
            requester_id=requester_id,
        ),
    )

    # #404: a request that reached here having been checked against a declaration
    # (``declared is not None``) is not just answered, it is granted -- the same
    # firewall rules launch would have written for this network had it resolved by
    # then. A caller this node could not identify as a local instance (``declared is
    # None``, e.g. another node asking as a peer) has no VM here to open anything
    # on, so it keeps the informational-only answer it always had.
    if declared is not None and requester_id is not None:
        grant_resolved_network(vmachine_id=requester_id, resolution=resolution)

    return resolution


def match_networks(a: celaut.Service.Network, b: celaut.Service.Network) -> bool:
    """Whether two ``Service.Network`` declarations name the same communication domain.

    A ``Network`` is a tags/prose/formal descriptor like every other replaceable
    component in celaut, so it is compared by the rule every other one is compared by
    (``node_identity.same_component``, and see ``Peer.SignatureScheme`` in
    celaut.proto): **formal decides whenever both sides declare one**, and otherwise
    one shared tag is enough.

    Reading ``formal`` is the point. Until now this was a tag intersection with a
    ``# TODO Could be more powerfull`` on it, which was harmless while nothing put
    anything in ``formal`` -- and stopped being harmless the moment a ``pow:ergo``
    network started carrying its actual ask there (issue #78). Two services both
    tagged ``pow:ergo`` asking for different blocks and different amounts of work are
    not in the same domain, and the ancestor chain was authorizing one as the other.

    What that costs, stated plainly, because it is a real narrowing of
    :func:`filter_networks_with_ancestors`: a parent that declares a ``formal`` grants
    its descendants **that** ask and no other, byte for byte, since two formals either
    are the same bytes or are not. A parent meaning to grant a family of asks -- any
    ``pow:ergo`` network its children care to specify -- says so by declaring the tag
    and leaving ``formal`` empty, which is the same thing it already meant. Nothing
    that declares no ``formal`` anywhere changes behaviour at all.

    Not ``same_component_stack``: that pairs up a *stack* of descriptors, and a
    Network is one descriptor. ``protocol_stack`` is not compared here either -- it
    says what the peers are expected to speak, not which domain this is, and a parent
    and child that name the same domain in different protocol terms are still naming
    the same domain.
    """
    return same_component(a, b)

def filter_networks_with_ancestors(networks: List[celaut.Service.Network], father_id: str) -> List[celaut.Service.Network]:
    """Keep only the networks that every ancestor of ``father_id`` also declares.

    This is the authorization control for ``Service.Network``: a network is usable
    only if every ancestor declares one that :func:`match_networks` accepts -- which
    reads ``formal`` when both sides carry one, and falls back to a shared tag. The AND over the chain is
    "only the direct father authorizes" applied by induction -- a father can only
    grant the domain its own father granted it, recursively -- so the walk
    re-derives the effective grant from each ancestor's spec. (It re-derives it
    because what is read is the ancestor's *spec*, i.e. what it asked for, not a
    persisted record of what it was actually granted.)

    An ancestor's spec that cannot be read raises and aborts the launch, because a
    spec the node failed to load is not a spec that declared no restrictions. Both
    ways of failing to read one end that way, each on its own grounds: an
    unloadable spec may well load on a retry, and a spec missing for a service
    this node launched is an inconsistent registry, since the launch path stores
    every spec it runs before running it. Telling the two apart is why the spec
    comes from ``load_service_from_disk`` and not from ``read_service_from_disk``,
    whose ``None`` covers both. Answering that ``None`` by handing the caller its
    own list back skipped this generation's check *and* every ancestor above it --
    the return preceded the recursion -- and its transient half, a timeout waiting
    to unlock memory, made the control degrade to allow-all exactly when the node
    was loaded enough to be pushed there (#269).
    """
    # Nothing left to authorize. No ancestor can subtract from an empty grant, so
    # the walk stops instead of reading specs whose answer cannot matter -- which
    # also keeps a launch that asks for no network at all from depending on the
    # registry being readable.
    if not networks:
        return []

    filtered = []
    service_id = sc.get_service_id_by_container_id(id=father_id)

    try:
        spec = load_service_from_disk(service_hash=service_id)
    except ServiceSpecUnavailable as e:
        raise NetworkAuthorizationError(
            f"Cannot authorize networks for {father_id}: the spec of its service "
            f"{service_id} is on the registry but was not loadable ({e}). Nothing is "
            "granted; the launch has to be retried when the node is less loaded."
        ) from e
    except ServiceNotInRegistry as e:
        raise NetworkAuthorizationError(
            f"Cannot authorize networks for {father_id}: its service {service_id} is not "
            f"on the local registry ({e}), so what that generation was allowed to reach "
            "cannot be re-derived. Every spec this node launches is stored first, so a "
            "missing one is an inconsistent registry, not a normal state."
        ) from e

    for network in networks:
        for spec_net in spec.network:
            if match_networks(network, spec_net):
                filtered.append(network)
                break  # Exit the inner loop

    ancestor_id = sc.get_internal_father_id(id=father_id)
    if sc.internal_instance_exists(id=ancestor_id):
        filtered = filter_networks_with_ancestors(networks=filtered, father_id=ancestor_id)

    return filtered
