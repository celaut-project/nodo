"""Proof-of-Work communication domains: ``pow:<chain>`` networks.

A ``Service.Network`` whose tag is ``pow:ergo`` names a *class* of domain -- the
Ergo PoW network -- and its ``formal`` field says which instance of that class is
meant: "peers whose main chain contains block B and carries at least D cumulative
work". Tags alone cannot express that, which is why this is the first network kind
in nodo that reads ``formal`` at all (see
``docs/proposals/78-network-guarantees-and-pow.md``, issue #78).

``formal`` is the ``key=value`` body every other celaut component declares one in
(``node_identity.component_formal``): sorted lines, UTF-8, compared byte for byte.
Not a shape invented here -- a signature scheme's curve and an address's transport
already state their determinate parameters this way, and a PoW requirement is the
same kind of statement about the same kind of field. It costs a reader nothing to
parse (``celaut-project/skills`` is JS with no protobuf dependency), it is diffable
by eye in a service spec, and it makes the bytes canonical by construction, which
neither a JSON object nor a protobuf map is -- map serialization is explicitly not
canonical, which is why this repo's own ``canonical_peer_content_digest`` refuses
``SerializeToString()``.

The domain's own keys are prefixed ``pow.``, so the vocabulary this module reads is
namespaced from anything else the same descriptor carries. **Unrecognized keys are
preserved, not refused**: a key outside this vocabulary rides alongside the known
ones and is round-tripped by :func:`canonical_formal`, and it is emphatically *not*
treated as a constraint this node enforces. What is still refused is a missing
required key or a malformed value -- validation of what is understood, which is a
different thing from rejecting what is not.

There is no version key. A version belongs to the vocabulary being spoken, and that
is named in the network's sibling ``protocol_stack`` descriptor, not duplicated in
here where the two could disagree. For the same reason ``protocol`` and
``peerDiscovery`` are not keys of this body: each is a tags/prose/formal descriptor
in its own right, which is exactly what ``Service.Network.protocol_stack``
(``repeated Api.Protocol``) already models.

What v1 verifies is **self-reported**: a candidate's own REST answers about its own
state. That catches the overwhelmingly common failure -- an out-of-sync, stalled,
wrong-network or pruned node -- and does not catch a deliberate liar. The proposal
document argues why shipping that is still strictly better than the
``resolve_ergo_network() -> []`` it replaces, and what v2 (cross-checking k of n
candidates) and v3 (verifying the Autolykos solutions) would add. Nothing here
should be read as a consensus check.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from protos import celaut_pb2 as celaut
from src.identity.node_identity import (
    ComponentFormalError,
    component_formal,
    parse_component_formal,
)
from src.manager.ergo import MAINNET_P2P_PORT, MAINNET_REST_PORT
from src.manager.network_defaults import configured_endpoints
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

env_manager = ConfigManager()

#: Tag prefix that routes a network to this module. Deliberately contains no ``.``,
#: so such a tag can never fall into ``resolve_network``'s DNS heuristic
#: (``not tag.islower() or '.' not in tag``).
POW_TAG_PREFIX = "pow:"

#: Prefix of every key this module claims. Namespaced so a ``formal`` can carry a
#: neighbouring vocabulary's keys without either side having to know about the
#: other, which is the whole point of not refusing what is not recognized.
KEY_PREFIX = "pow."

_REQUIRED_KEYS = tuple(
    KEY_PREFIX + name for name in ("chain", "block_id", "min_cumulative_difficulty")
)
_OPTIONAL_KEYS = tuple(KEY_PREFIX + name for name in ("min_height", "max_tip_age_s"))

#: Not a whitelist. It separates the keys this module *interprets* from the ones it
#: merely carries (:attr:`PowRequirement.extensions`); nothing is refused for being
#: outside it.
_DOMAIN_KEYS = frozenset(_REQUIRED_KEYS + _OPTIONAL_KEYS)

#: Chains this module can parse. Parsing and resolving are separate capabilities:
#: ``bitcoin`` parses (so an ancestor-chain comparison can be written against it)
#: and does not resolve (see ``resolve_pow_network``).
KNOWN_CHAINS = ("ergo", "bitcoin")

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_MAX_PEERS = 8

#: What ``resolve_pow_network`` names its own two slots in each ``Instance`` it
#: builds, in ``Api.Slot.protocol_stack`` -- never echoed from ``network.protocol_stack``
#: (which is the *requester's* ask, identical across every candidate of one
#: resolution, and not observed of the peer at all; see the proposal document). These
#: two are this node's own, peer-specific statement of which port speaks which half:
#: the P2P port a chain guest actually wants, and the REST one this node itself just
#: verified the candidate at. ``narrow_instances_for_local_grant`` reads them back to
#: decide what a local guest's firewall grant may keep.
P2P_SLOT_TAG = "ergo-p2p"
REST_SLOT_TAG = "ergo-rest"


class PowFormalError(ValueError):
    """``Network.formal`` could not be read as a PoW requirement.

    Raised rather than returning None: a requirement this node cannot parse is not
    a requirement that asked for nothing.
    """


@dataclass(frozen=True)
class PowRequirement:
    """What a ``pow:<chain>`` network asks of a peer.

    ``min_cumulative_difficulty`` is **cumulative work since genesis** (Ergo's
    ``fullBlocksScore``, Bitcoin's ``chainwork``), not the difficulty of the tip
    block. That is the quantity the chain's own fork-choice rule maximises, it is
    monotone -- so a requirement written today does not flip validity at the next
    retarget -- and it is a total order, so two peers are comparable. It is carried
    as ``int`` and never as a float: Ergo's score passed 2**64 long ago.

    ``extensions`` are the keys that were in the body and are not this module's:
    carried so :func:`canonical_formal` gives back what it was handed, and read by
    nothing here. They are ``str`` because the body is text -- there is no opaque
    half of a ``key=value`` line.
    """

    chain: str
    block_id: Optional[str]
    min_cumulative_difficulty: Optional[int]
    min_height: Optional[int] = None
    max_tip_age_s: Optional[int] = None
    extensions: Dict[str, str] = field(default_factory=dict)
    templated: Tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """Whether this requirement can be put to a peer at all.

        A templated requirement (``pow.block_id=${ERGO_BLOCK_ID}``, #385) states an
        intention and not an ask: there is no block to look for until the
        instantiator names one. ``block_id`` and ``min_cumulative_difficulty`` are
        therefore ``Optional`` -- they are ``None`` exactly when the key that carries
        them was left open -- and this is the one question a caller has to ask before
        reading them.

        Only :func:`parse_pow_formal` with ``allow_templates=True`` can build an
        incomplete one, which is the pack-time and subset-check path. The resolver
        never asks for that, so by the time a requirement reaches
        :func:`ergo_peer_satisfies` it is complete by construction.
        """
        return not self.templated


def _is_placeholder(value: str) -> bool:
    """Whether one ``formal`` value is an unfilled ``${VAR}`` selection key (#385).

    Imported from :mod:`src.manager.network_templates` so the grammar is stated once:
    this module, the packer and the gateway's subset check all have to agree on what
    counts as "left open", and three regexes would be three chances not to.
    """
    from src.manager.network_templates import PLACEHOLDER

    return bool(PLACEHOLDER.fullmatch(value.strip()))


def _as_int(value: str, field: str) -> int:
    """A non-negative base-10 integer from one ``formal`` value.

    Every value in a ``key=value`` body is text, which is exactly what cumulative
    work needs: Ergo's score passed 2**64 long ago, and an encoding whose numbers
    are IEEE doubles would have rounded it away before this function ever saw it.
    So there is one accepted spelling and no numeric type to be lenient about.
    """
    text = value.strip()
    if not text:
        raise PowFormalError(f"Network.formal: '{field}' is empty.")
    try:
        parsed = int(text, 10)
    except ValueError:
        raise PowFormalError(
            f"Network.formal: '{field}' must be a base-10 integer, got {value!r}."
        ) from None
    if parsed < 0:
        raise PowFormalError(f"Network.formal: '{field}' must not be negative, got {parsed}.")
    return parsed


def parse_pow_formal(
    formal: bytes,
    tag: Optional[str] = None,
    allow_templates: bool = False,
) -> PowRequirement:
    """Read ``Network.formal`` as a :class:`PowRequirement`.

    ``tag`` is checked against the declared chain when given: a ``pow:ergo`` tag
    carrying ``pow.chain=bitcoin`` is a malformed specification, not a cross-chain
    request, and reading it either way would mean resolving one chain for a tag the
    operator's policy vetted as another.

    ``allow_templates`` (#385) accepts ``${VAR}`` as the value of any key that would
    otherwise need a concrete hash or integer, recording it in
    :attr:`PowRequirement.templated` and leaving the typed field ``None``. It
    defaults to **False**, so the two callers that intend to *resolve* something --
    :func:`resolve_pow_network` and, through it, ``resolve_network`` -- cannot
    silently get an incomplete requirement and go looking for a peer that contains
    block ``${ERGO_BLOCK_ID}``. It is passed ``True`` by the packer, which is
    validating what the author wrote and not asking anyone anything, and by the
    ``ResolveNetwork`` subset check, which is comparing two declarations.

    ``pow.chain`` is **never** templatable, whatever ``allow_templates`` says. It is
    an identity key, not a selection key: it is the half of the declaration the
    operator's ``service_networks`` policy vets (through the tag it must agree with),
    and a chain chosen at instantiation time is a policy decision made after the
    policy ran. Everything this module can select on -- which block, how much work,
    how high, how fresh -- is selection and may be left open.

    Keys outside the ``pow.`` vocabulary are kept in ``extensions`` and otherwise
    ignored. They are not refused: refusing them would make every reader the ceiling
    on what a descriptor may say, and this one enforces none of them, so there is
    nothing it would be granting by not understanding them. What it does refuse is a
    body it cannot read at all, a missing ``pow.`` key it needs, or a value of one
    that is not what that key is defined to hold.
    """
    if not formal:
        raise PowFormalError(
            "Network.formal is empty. A pow: network has to say which block and how "
            "much work it means; the tag alone names only the chain."
        )

    try:
        body = parse_component_formal(formal)
    except ComponentFormalError as e:
        raise PowFormalError(f"Network.formal is not a key=value body: {e}") from None

    document = {key: value for key, value in body.items() if key in _DOMAIN_KEYS}
    extensions = {key: value for key, value in body.items() if key not in _DOMAIN_KEYS}

    missing = [key for key in _REQUIRED_KEYS if key not in document]
    if missing:
        raise PowFormalError(f"Network.formal is missing: {', '.join(missing)}.")

    templated = tuple(
        sorted(
            key
            for key, value in document.items()
            if key != "pow.chain" and _is_placeholder(value)
        )
    )
    if templated and not allow_templates:
        raise PowFormalError(
            f"Network.formal still carries unfilled selection templates: "
            f"{', '.join(templated)}. A `${{VAR}}` is answered from the launching "
            "instance's environment_variables before the network is resolved "
            "(see src/manager/network_templates.py); reaching a resolver with one "
            "still in place means it was never answered, and there is no peer that "
            "holds a block named after a variable."
        )

    chain = document["pow.chain"]
    if _is_placeholder(chain):
        raise PowFormalError(
            "Network.formal: 'pow.chain' must not be templated. It is an identity "
            "key -- it says which protocol this is, it has to agree with the "
            "`pow:<chain>` tag the operator's policy vetted, and a chain chosen "
            "after that policy ran is a chain nobody vetted."
        )
    if not chain.strip() or chain.strip() != chain.strip().lower():
        raise PowFormalError(
            f"Network.formal: 'pow.chain' must be a lowercase non-empty value, got {chain!r}."
        )
    chain = chain.strip()
    if chain not in KNOWN_CHAINS:
        raise PowFormalError(
            f"Network.formal: unknown chain {chain!r}. Known: {', '.join(KNOWN_CHAINS)}."
        )

    if tag is not None:
        declared = tag[len(POW_TAG_PREFIX):] if tag.startswith(POW_TAG_PREFIX) else tag
        if declared != chain:
            raise PowFormalError(
                f"Network tag {tag!r} names chain {declared!r} but its formal says "
                f"{chain!r}. The tag is what the operator's policy vetted, so the two "
                "have to agree."
            )

    open_keys = frozenset(templated)

    def typed_int(key: str) -> Optional[int]:
        """The integer at ``key``, or None when the author left that key open."""
        if key in open_keys or key not in document:
            return None
        return _as_int(document[key], key)

    if "pow.block_id" in open_keys:
        block_id = None
    else:
        block_id = document["pow.block_id"].strip().lower()
        if not block_id or any(c not in "0123456789abcdef" for c in block_id):
            raise PowFormalError(
                f"Network.formal: 'pow.block_id' is not hexadecimal: "
                f"{document['pow.block_id']!r}."
            )

    return PowRequirement(
        chain=chain,
        block_id=block_id,
        min_cumulative_difficulty=typed_int("pow.min_cumulative_difficulty"),
        min_height=typed_int("pow.min_height"),
        max_tip_age_s=typed_int("pow.max_tip_age_s"),
        extensions=extensions,
        templated=templated,
    )


def canonical_formal(requirement: PowRequirement) -> bytes:
    """The requirement back as ``formal`` bytes: :func:`component_formal`'s sorted lines.

    Canonical by construction rather than by convention -- ``component_formal`` sorts
    the keys, so the same requirement built in any order is the same bytes. That is
    what makes a ``formal`` hashable, comparable and loggable reproducibly, and it is
    what ``match_networks`` compares down the ancestor chain.

    Extensions go back out with everything else, so a body that travelled through this
    node comes out saying what it came in saying. A key it does not interpret is still
    part of what the author declared, and dropping it here would quietly turn a
    round-trip into an edit.

    A key the author left templated (#385) cannot be written back, because
    :class:`PowRequirement` deliberately does not keep the variable *name* -- it keeps
    which keys were open, which is all any reader here needs. Re-serializing one would
    therefore either invent a name or drop the key, and dropping it is the dangerous
    half: ``pow.block_id`` silently vanishing turns "contains block B" into "any
    peer". So it refuses, and the caller that wants the templated bytes uses the ones
    it already has -- the declaration -- which is what both such callers (the packer
    and the subset check) are holding anyway.
    """
    if requirement.templated:
        raise PowFormalError(
            "A templated requirement cannot be re-serialized: "
            f"{', '.join(requirement.templated)} were left open and this object does "
            "not carry the variable names. Serialize the declared formal bytes, or "
            "substitute first (src/manager/network_templates.py)."
        )
    pairs: Dict[str, str] = {
        "pow.chain": requirement.chain,
        "pow.block_id": requirement.block_id,
        "pow.min_cumulative_difficulty": str(requirement.min_cumulative_difficulty),
    }
    if requirement.min_height is not None:
        pairs["pow.min_height"] = str(requirement.min_height)
    if requirement.max_tip_age_s is not None:
        pairs["pow.max_tip_age_s"] = str(requirement.max_tip_age_s)
    overridden = sorted(set(requirement.extensions) & _DOMAIN_KEYS)
    if overridden:
        # Not reachable from `parse_pow_formal`, which partitions by the same set.
        # Reachable from a hand-built PowRequirement, where it would mean the
        # serializer silently dropping one of the two values for a key.
        raise PowFormalError(
            f"Extensions must not override this module's own keys: {', '.join(overridden)}."
        )
    pairs.update(requirement.extensions)
    return component_formal(pairs)


# --------------------------------------------------------------------- candidates

#: Config block. Not ``networks``: that would sit one letter from the unrelated
#: ``network:`` block, which is this node's own ports and addresses -- the same
#: reason ``src/utils/network_policy.py`` calls its block ``service_networks``.
CONFIG_BLOCK = "pow_networks"


def _timeout() -> int:
    try:
        return int(env_manager.get(f"{CONFIG_BLOCK}.TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _max_peers() -> int:
    try:
        value = int(env_manager.get(f"{CONFIG_BLOCK}.MAX_PEERS", DEFAULT_MAX_PEERS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PEERS
    return value if value > 0 else DEFAULT_MAX_PEERS


def _configured_endpoints(tag: str) -> List[str]:
    """Use the same operator defaults as every other communication domain."""
    return configured_endpoints(tag, config=env_manager)


def _peer_suggested_endpoints(network: celaut.Service.Network) -> List[str]:
    """What other celaut nodes answer for this domain, as REST URLs to *try*.

    Imported where it is used: it pulls in the database and the gRPC transport, which a
    node resolving a DNS network has no reason to be loading here.

    ``ask_peers`` gives ``(ip, port, tags)`` triples read off the ``Instance.Uri`` of a
    peer's ``ResolveNetwork`` answer. A peer following this same module's convention
    (:func:`resolve_pow_network`) tags its REST slot with :data:`REST_SLOT_TAG`, and
    that port -- looked up first, per ip -- is used directly: it is exactly the
    address that peer says answers REST, no better or worse trusted than any other
    address here, and dropped by the same verification below if it is wrong. Only an
    ip nobody tagged that way (an older nodo, or another celaut implementation that
    never built a REST slot) falls back to :data:`src.manager.ergo.MAINNET_REST_PORT`
    -- the same guess this module always made, now the last resort instead of the
    only option. (The scheme is guessed too, and for the same reason:
    ``Instance.Uri`` carries no scheme, so an https-only peer is one more candidate
    this costs a single failed request, not a guest.)
    """
    try:
        from src.manager.network_discovery import ask_peers
    except Exception as e:  # pragma: no cover - environment-dependent
        logger(f"[POW] peer endpoint source unavailable: {type(e).__name__}: {e}")
        return []

    suggested = ask_peers(network)
    rest_port_by_ip = {
        ip: port for ip, port, tags in suggested if REST_SLOT_TAG in tags
    }

    urls: List[str] = []
    seen_ips = set()
    for ip, _port, _tags in suggested:
        if ip in seen_ips:
            continue
        seen_ips.add(ip)
        urls.append(f"http://{ip}:{rest_port_by_ip.get(ip, MAINNET_REST_PORT)}")
    return urls


def candidate_urls(
    network: celaut.Service.Network,
    tag: str,
    ask_peers: bool = True,
) -> List[str]:
    """Candidate chain endpoints for one ``pow:`` network, most trusted first.

    Four sources. The order is a trust order and nothing else -- **every candidate is
    verified identically whatever named it** (:func:`ergo_peer_satisfies`), so the order
    decides only who is asked first and therefore who fills the ``MAX_PEERS`` budget:

    1. ``ledgers.ergo.NODE_URL`` -- the node this operator already trusts with
       reputation reads and payment proofs.
    2. ``service_networks.default_instances[<tag>]`` -- what the operator wrote down for this tag.
    3. Other celaut nodes (:func:`_peer_suggested_endpoints`), over
       ``Gateway.ResolveNetwork``. Peers this node holds a relationship with, who have
       already done this finding for themselves, but who staked nothing on the answer.
    4. ``ledgers.ergo.HTTP_PEERS_PATH`` -- the crawl in ``src/manager/ergo.py``.
       Strangers who paid nothing and were asked nothing. The file is **read**, never
       refreshed from here: the crawl recurses unboundedly and a service launch is not
       the place to start one.

    That a stranger can put an address in front of this node is why nothing here grants
    anything. The cost of a lie at this layer is one wasted HTTP request; the firewall
    rule is written later, and only for a peer that answered the requirement.

    ``ask_peers=False`` drops source 3, and is not a tuning knob: it is what stops one
    ``ResolveNetwork`` call turning into a flood. Answering a peer's question by asking
    our peers -- who ask theirs -- is a cycle in a graph nobody has a view of, and two
    nodes that know each other are a cycle of length two. A node therefore answers from
    what it knows locally and never by relaying, so one question costs one node one
    round of verification. See ``gateway.Gateway.ResolveNetwork``.
    """
    urls: List[str] = []

    configured = str(env_manager.get("ledgers.ergo.NODE_URL", "") or "").strip()
    if configured:
        urls.append(configured)

    urls.extend(_configured_endpoints(tag))
    if ask_peers:
        urls.extend(_peer_suggested_endpoints(network))

    peers_path = str(env_manager.get("ledgers.ergo.HTTP_PEERS_PATH", "") or "").strip()
    if peers_path and os.path.exists(peers_path):
        try:
            with open(peers_path, "r", encoding="utf-8") as handle:
                crawled = json.load(handle)
            # The keys are `restApiUrl` values as `src/manager/ergo.py` wrote them,
            # i.e. full URLs like "https://node.example:9053" -- NOT "ip:port". The
            # dead code in `resolve_ergo_network` split them on ":" and would have
            # raised on every entry; see the proposal document.
            if isinstance(crawled, dict):
                urls.extend(str(url).strip() for url in crawled if str(url).strip())
        except (OSError, ValueError) as e:
            logger(f"[POW] could not read {peers_path}: {type(e).__name__}: {e}")

    seen = set()
    ordered = []
    for url in urls:
        normalized = url.rstrip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def crawled_p2p_addresses() -> Dict[str, str]:
    """``restApiUrl -> "host:port"`` for every crawl entry that recorded a P2P endpoint.

    The crawl (``src/manager/ergo.py``) is the one source that *observes* a peer's P2P
    address: ``/peers/connected`` entries carry ``address`` next to ``restApiUrl``, and
    the crawl now keeps it as ``p2pAddress``. Read separately from :func:`candidate_urls`
    rather than folded into it because it answers a different question -- that function
    decides *who to ask*, this one *where the one we asked actually speaks the chain*.

    Backward compatible in both directions: an entry written by an older nodo has no
    ``p2pAddress`` and simply contributes nothing, which is the same position every
    non-crawl candidate is in.
    """
    peers_path = str(env_manager.get("ledgers.ergo.HTTP_PEERS_PATH", "") or "").strip()
    if not peers_path or not os.path.exists(peers_path):
        return {}
    try:
        with open(peers_path, "r", encoding="utf-8") as handle:
            crawled = json.load(handle)
    except (OSError, ValueError) as e:
        logger(f"[POW] could not read {peers_path}: {type(e).__name__}: {e}")
        return {}
    if not isinstance(crawled, dict):
        return {}

    addresses: Dict[str, str] = {}
    for url, entry in crawled.items():
        if not isinstance(entry, dict):
            continue
        address = entry.get("p2pAddress")
        if isinstance(address, str) and address.strip():
            addresses[str(url).strip().rstrip("/")] = address.strip()
    return addresses


def _p2p_uri_for(url: str, p2p_address: Optional[str] = None) -> Optional[Tuple[str, int]]:
    """``(ip, port)`` of a verified candidate's **P2P** endpoint, or None.

    Two cases, and the difference between them is whether anything ever saw the port:

    * ``p2p_address`` given (``"1.2.3.4:9030"``, from the crawl's ``/peers/connected``
      ``address``): host and port both come from it. This is an observation, and it is
      the case where a peer on a non-conventional port is still reached correctly.
    * ``p2p_address`` absent -- ``ledgers.ergo.NODE_URL``, ``default_instances``, a peer's
      ``ResolveNetwork`` answer, or a crawl entry an older nodo wrote. The REST host is
      reused with :data:`src.manager.ergo.MAINNET_P2P_PORT`, and **the assumption is
      logged**, because the port is the one thing here nobody checked.

    Reusing the REST *host* is not an assumption of the same kind: it is where the node
    that answered ``/info`` lives. Only the port is being guessed.

    Verification is unaffected and stays on the REST URL (:func:`ergo_peer_satisfies`):
    what is being answered is "does this node's chain contain B", which only the REST
    API can say. This function decides what the *guest* is then allowed to dial.
    """
    if p2p_address:
        host, _, port_text = p2p_address.rpartition(":")
        host = host.strip().strip("[]")
        try:
            port = int(port_text)
        except ValueError:
            port = 0
        if host and 0 < port < 65536:
            return _resolve_host(host, port)
        logger(f"[POW] {url}: unusable p2pAddress {p2p_address!r}; falling back to the default port")

    parsed = urlparse(url if "//" in url else f"//{url}")
    host = parsed.hostname
    if not host:
        return None
    port = MAINNET_P2P_PORT
    logger(
        f"[POW] {url}: no observed P2P address; assuming {host}:{port} "
        f"(Ergo mainnet's conventional P2P port)"
    )
    return _resolve_host(host, port)


def _resolve_host(host: str, port: int) -> Optional[Tuple[str, int]]:
    """``(ip, port)`` for a host that may be a name. Shared by both uri builders.

    ``getaddrinfo`` on a numeric literal is a parse, not a lookup, so the crawl's
    already-numeric addresses cost nothing and take no DNS caveat.
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as e:
        logger(f"[POW] cannot resolve {host}: {e}")
        return None
    for info in infos:
        return info[4][0], int(port)
    return None


def _rest_uri_for(url: str) -> Optional[Tuple[str, int]]:
    """``(ip, port)`` of a verified candidate's own **REST** endpoint.

    Unlike the P2P port, this one is never guessed: ``url`` is exactly the address
    ``ergo_peer_satisfies`` just verified the candidate at, in the loop this is
    called from, so its host and port are already known -- only pinning to an
    address (as the P2P uri also does) is left to do. No port in ``url`` falls back
    to the scheme's own default (80/443), which is what ``requests`` -- and
    therefore the verification that just ran -- used too.
    """
    parsed = urlparse(url if "//" in url else f"//{url}")
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme, MAINNET_REST_PORT)
    return _resolve_host(host, port)


# -------------------------------------------------------------------- verification

def _get_json(url: str, path: str, timeout: int) -> Optional[Any]:
    """One GET, returning None for "this peer did not answer that" of any kind.

    A 404 is a *finding* (the peer does not have the block) and an unreachable host
    is an absence, but neither can qualify a peer, so both collapse to None here and
    the caller reads them the same way: not a peer for this network.
    """
    try:
        response = requests.get(f"{url}{path}", timeout=timeout)
    except requests.exceptions.RequestException as e:
        logger(f"[POW] {url}{path}: {type(e).__name__}")
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def ergo_peer_satisfies(
    url: str,
    requirement: PowRequirement,
    timeout: Optional[int] = None,
    now_ms: Optional[int] = None,
) -> bool:
    """Whether one Ergo REST endpoint meets ``requirement``, by its own account.

    Five questions, in increasing cost, so a peer that fails a cheap one is never
    asked an expensive one:

    1. Is it even on our chain? ``/info.genesisBlockId`` against
       ``ledgers.ergo.GENESIS_BLOCK_ID`` -- the check ``src/manager/ergo.py``
       already makes, because a testnet node is not a peer on this network.
    2. Has enough work gone into the chain it follows? ``fullBlocksScore``, exact
       integer comparison. ``headersScore`` is deliberately NOT accepted: a
       header-only chain is not one this peer can serve blocks from.
    3. Is it high enough? ``fullHeight`` against ``min_height``.
    4. Does it have the block at all? ``/blocks/{id}/header`` -- 200 or 404.
    5. Is the block on its **main** chain? ``/blocks/at/{height}`` must list it.
       Without this a peer that merely stored an orphan passes, which is exactly
       the case "contains a given block" is meant to exclude.

    ``max_tip_age_s`` is judged against the block header's own timestamp and *our*
    clock, never the peer's ``currentTime``: a stalled node reporting a fresh clock
    is the state this check exists to catch.
    """
    timeout = _timeout() if timeout is None else timeout

    info = _get_json(url, "/info", timeout)
    if not isinstance(info, dict):
        return False

    expected_genesis = str(env_manager.get("ledgers.ergo.GENESIS_BLOCK_ID", "") or "").strip()
    if expected_genesis and str(info.get("genesisBlockId") or "") != expected_genesis:
        logger(
            f"[POW] {url} is not on the configured chain "
            f"(genesis {info.get('genesisBlockId')!r}, expected {expected_genesis!r})"
        )
        return False

    try:
        score = int(str(info.get("fullBlocksScore")))
    except (TypeError, ValueError):
        logger(f"[POW] {url} reported no usable fullBlocksScore")
        return False
    if score < requirement.min_cumulative_difficulty:
        return False

    if requirement.min_height is not None:
        try:
            full_height = int(info.get("fullHeight"))
        except (TypeError, ValueError):
            return False
        if full_height < requirement.min_height:
            return False

    header = _get_json(url, f"/blocks/{requirement.block_id}/header", timeout)
    if not isinstance(header, dict):
        return False
    try:
        height = int(header.get("height"))
    except (TypeError, ValueError):
        return False

    at_height = _get_json(url, f"/blocks/at/{height}", timeout)
    if not isinstance(at_height, list):
        return False
    if requirement.block_id not in {str(entry).lower() for entry in at_height}:
        logger(
            f"[POW] {url} knows block {requirement.block_id} but it is not on its main "
            f"chain at height {height}"
        )
        return False

    if requirement.max_tip_age_s is not None:
        tip = _get_json(url, "/blocks/lastHeaders/1", timeout)
        if not isinstance(tip, list) or not tip or not isinstance(tip[0], dict):
            return False
        try:
            tip_ms = int(tip[0].get("timestamp"))
        except (TypeError, ValueError):
            return False
        reference_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if reference_ms - tip_ms > requirement.max_tip_age_s * 1000:
            logger(f"[POW] {url} tip is older than max_tip_age_s={requirement.max_tip_age_s}")
            return False

    return True


# ---------------------------------------------------------------------- resolution

def resolve_pow_network(
    network: celaut.Service.Network,
    tag: Optional[str] = None,
    ask_peers: bool = True,
) -> List[celaut.Instance]:
    """Peers that satisfy a ``pow:<chain>`` network, **one Instance per endpoint**.

    One per endpoint because that is what they are: separate operators, separately
    verified, separately reachable, and separately worth dropping. Packing them into one
    Instance's ``uri`` list would say the opposite -- that shape means "one peer at
    several addresses", which is what ``resolve_domain`` legitimately builds out of the A
    records of a single name -- and it would leave the guest unable to tell the peers
    apart in its own ``__config__``.

    **Each Instance carries two slots, tagged apart: its P2P endpoint and its REST
    one.** A service declaring ``pow:ergo`` is asking for chain peers -- something to
    sync a chain against -- and the REST API is not that: it is the interface this node
    used to *check* the peer, on a port no chain protocol is spoken on. The two are not
    interchangeable, so each is its own slot (:data:`P2P_SLOT_TAG` /
    :data:`REST_SLOT_TAG` in ``Api.Slot.protocol_stack``, never the requester's own
    ``network.protocol_stack`` echoed onto either one -- that describes what the
    *asker* wants, identically for every candidate, not what a specific peer's port
    speaks). Building both, always, is what lets this node's own peer discovery
    (``network_discovery.ask_peer``) verify a suggestion by its REST port when another
    node names one, instead of guessing at Ergo mainnet's conventional REST port
    every time (:data:`src.manager.ergo.MAINNET_REST_PORT`, see
    ``pow_networks._peer_suggested_endpoints``).

    **A local guest's firewall grant is not the same view.** Emitting the REST address
    to a *guest* that only asked for a chain peer would be granting egress that could
    not be used for what it was granted for -- both useless and open, the one thing a
    firewall rule must not be. So the Instance handed back here always carries both
    slots, and :func:`narrow_instances_for_local_grant` is what
    ``networks.resolve_network_for_peer`` calls to strip the REST one back out before
    a local caller's ``NetworkResolution`` is built and its firewall opened -- a remote
    node asking as a peer opens nothing on the strength of any answer, so it keeps the
    full picture.

    Where the P2P port comes from: this node does not assume one for a network wherever
    it can observe it. The crawl observes each peer's P2P address
    (``/peers/connected.address``) and that is what is emitted, so a peer on 9031 is
    reached on 9031. Only a candidate whose P2P endpoint nobody ever saw --
    ``NODE_URL``, ``default_instances``, a peer's ``ResolveNetwork`` answer -- falls
    back to Ergo mainnet's conventional P2P port
    (:data:`src.manager.ergo.MAINNET_P2P_PORT`, 9030), and that fallback is logged
    where it happens. The REST port is never guessed here: it is the very address
    ``ergo_peer_satisfies`` just verified the candidate at, a line above.

    That this is safe took a fix at the other end: ``configure_guest_firewall_policy``
    stopped at the first peer instance it could write a rule for, so N instances would
    have opened the first peer and silently dropped the rest. It now writes a rule for
    every one of them, which is what the guest is told it may reach.

    No qualifying peer returns ``[]`` rather than raising: "nobody meets D right now" is
    a statement about the world and a transient one, unlike a policy rejection (intent)
    or an unreadable ancestor spec (this node's integrity), both of which do abort. The
    reasoning is in the proposal document.
    """
    pow_tag = tag
    if pow_tag is None:
        pow_tag = next((t for t in network.tags if t.startswith(POW_TAG_PREFIX)), None)
    if pow_tag is None:
        return []

    requirement = parse_pow_formal(network.formal, tag=pow_tag)

    if requirement.chain != "ergo":
        # Parsed, and deliberately not resolved. nodo's default Bitcoin posture is
        # the receive-only Esplora backend, which exposes neither `chainwork` nor a
        # peer list, so there is no honest way to answer this ask from what the node
        # already has. A half-verification that returned peers anyway would be worse
        # than saying so.
        raise NotImplementedError(
            f"pow:{requirement.chain} networks parse but do not resolve yet. Only "
            "'ergo' is implemented; see docs/proposals/78-network-guarantees-and-pow.md."
        )

    timeout = _timeout()
    limit = _max_peers()
    peers: List[celaut.Instance] = []
    seen_addresses = set()
    p2p_addresses = crawled_p2p_addresses()

    for url in candidate_urls(network, pow_tag, ask_peers=ask_peers):
        if len(peers) >= limit:
            break
        if not ergo_peer_satisfies(url, requirement, timeout=timeout):
            continue
        address = _p2p_uri_for(url, p2p_addresses.get(url.rstrip("/")))
        if address is None:
            continue
        if address in seen_addresses:
            continue
        seen_addresses.add(address)

        # `internal_port` is the port a consumer of this slot would actually dial,
        # exactly as everywhere else in this node it names a real port rather than an
        # arbitrary index -- see `src/gateway/utils.py`'s own gateway-port slot.
        slots = [celaut.Service.Api.Slot(
            port=address[1],
            transport=celaut.Service.Api.Protocol(tags=["tcp"]),
            protocol_stack=[celaut.Service.Api.Protocol(tags=[P2P_SLOT_TAG])],
        )]
        uri_slots = [celaut.Instance.Uri_Slot(
            internal_port=address[1],
            uri=[celaut.Instance.Uri(ip=address[0], port=address[1])],
        )]

        rest_address = _rest_uri_for(url)
        # Distinct ports only: two slots sharing one internal_port would be two
        # Uri_Slot entries claiming the same key, which is not a shape anything here
        # (or a guest reading its own __config__) has a rule for resolving.
        if rest_address is not None and rest_address[1] != address[1]:
            slots.append(celaut.Service.Api.Slot(
                port=rest_address[1],
                transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                protocol_stack=[celaut.Service.Api.Protocol(tags=[REST_SLOT_TAG])],
            ))
            uri_slots.append(celaut.Instance.Uri_Slot(
                internal_port=rest_address[1],
                uri=[celaut.Instance.Uri(ip=rest_address[0], port=rest_address[1])],
            ))

        peers.append(
            celaut.Instance(
                api=celaut.Service.Api(slot=slots, payment_contracts=[]),
                uri_slot=uri_slots,
            )
        )

    if not peers:
        # Distinguishable in the log from the wildcard's empty answer and from an
        # AAAA-only DNS name, which are the other two ways `[]` is reached today.
        logger(
            f"[POW] no peer satisfies {pow_tag} (block {requirement.block_id}, "
            f"work >= {requirement.min_cumulative_difficulty}); resolving to no peers."
        )
        return []

    return peers


def narrow_instances_for_local_grant(
    instances: List[celaut.Instance],
    network: celaut.Service.Network,
) -> List[celaut.Instance]:
    """What a *local guest's* own resolution -- and its firewall grant -- gets to see.

    ``resolve_pow_network`` always builds both the P2P and the REST slot: a peer
    asking over ``Gateway.ResolveNetwork`` opens nothing on the strength of the
    answer, so it gets the full picture, and this node's own peer discovery
    (``network_discovery.ask_peer``) reads the REST one to verify a suggestion it
    would otherwise have to guess at. A **local guest** is different -- whatever
    Instance ends up in its ``NetworkResolution`` is also what
    ``networks.grant_resolved_network`` opens a firewall rule for (#404), and a guest
    that declared plain ``pow:ergo`` asked for a chain peer, not a REST hole next to
    it: exactly the "useless and open" shape the original design rejected (#78).

    So here the REST slot is kept only when the guest's *own* declared
    ``network.protocol_stack`` explicitly names it. A bare ``pow:ergo`` tag with no
    ``protocol_stack`` -- the overwhelming majority of real declarations -- gets
    exactly the P2P-only shape this returned before this feature existed. This is a
    narrower rule than ``networks._slot_exposes``'s (which treats an unstated
    ``protocol_stack`` as satisfied by any slot): that rule fits asking "is this
    instance a member of the network at all", where nothing declared is the loosest
    possible ask; here an unstated ``protocol_stack`` must keep meaning exactly what
    it always meant for this domain, not silently widen the moment a second slot
    exists to widen into.
    """
    wants_rest = any(REST_SLOT_TAG in protocol.tags for protocol in network.protocol_stack)
    if wants_rest:
        return instances

    narrowed = []
    for instance in instances:
        kept_slots = [
            slot for slot in instance.api.slot
            if not any(REST_SLOT_TAG in protocol.tags for protocol in slot.protocol_stack)
        ]
        kept_ports = {slot.port for slot in kept_slots}
        narrowed.append(celaut.Instance(
            api=celaut.Service.Api(
                slot=kept_slots,
                payment_contracts=instance.api.payment_contracts,
            ),
            uri_slot=[
                uri_slot for uri_slot in instance.uri_slot
                if uri_slot.internal_port in kept_ports
            ],
        ))
    return narrowed
