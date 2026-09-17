"""Proof-of-Work communication domains: ``pow:<chain>`` networks.

A ``Service.Network`` whose tag is ``pow:ergo`` names a *class* of domain -- the
Ergo PoW network -- and its ``formal`` field says which instance of that class is
meant: "peers whose main chain contains block B and carries at least D cumulative
work". Tags alone cannot express that, which is why this is the first network kind
in nodo that reads ``formal`` at all (see
``docs/proposals/78-network-guarantees-and-pow.md``, issue #78).

``formal`` contains a serialized ``celaut.NetworkFormal`` protobuf map. Known
values are UTF-8 strings with strict domain-specific validation; unknown entries
are preserved as opaque bytes, not interpreted as enforced constraints. New
mandatory semantics require a new supported version.

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
from google.protobuf.message import DecodeError
from protos.network_formal_pb2 import NetworkFormal
from src.manager.network_defaults import configured_endpoints
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

env_manager = ConfigManager()

#: Tag prefix that routes a network to this module. Deliberately contains no ``.``,
#: so such a tag can never fall into ``resolve_network``'s DNS heuristic
#: (``not tag.islower() or '.' not in tag``).
POW_TAG_PREFIX = "pow:"

#: The only ``formal`` version this node knows how to read. A newer one is refused
#: rather than best-effort parsed, for the reason in the module docstring.
SUPPORTED_VERSION = 1

_REQUIRED_KEYS = ("v", "chain", "block_id", "min_cumulative_difficulty")
_OPTIONAL_KEYS = ("min_height", "max_tip_age_s")
_KNOWN_KEYS = frozenset(_REQUIRED_KEYS + _OPTIONAL_KEYS)

#: Chains this module can parse. Parsing and resolving are separate capabilities:
#: ``bitcoin`` parses (so an ancestor-chain comparison can be written against it)
#: and does not resolve (see ``resolve_pow_network``).
KNOWN_CHAINS = ("ergo", "bitcoin")

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_MAX_PEERS = 8

#: Conventional Ergo node REST port, used when a ``restApiUrl`` names no port and
#: no scheme default applies.
ERGO_DEFAULT_PORT = 9053
_SCHEME_PORTS = {"http": 80, "https": 443}


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
    """

    chain: str
    block_id: str
    min_cumulative_difficulty: int
    min_height: Optional[int] = None
    max_tip_age_s: Optional[int] = None
    version: int = SUPPORTED_VERSION
    extensions: Dict[str, bytes] = field(default_factory=dict)


def _as_int(value: str, field: str) -> int:
    """A non-negative base-10 integer from one ``formal`` value.

    Every known value in the protobuf map is UTF-8 text, which is exactly what cumulative
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


def parse_pow_formal(formal: bytes, tag: Optional[str] = None) -> PowRequirement:
    """Read ``Network.formal`` as a :class:`PowRequirement`.

    ``tag`` is checked against the declared chain when given: a ``pow:ergo`` tag
    carrying ``chain=bitcoin`` is a malformed specification, not a cross-chain
    request, and reading it either way would mean resolving one chain for a tag the
    operator's policy vetted as another.
    """
    if not formal:
        raise PowFormalError(
            "Network.formal is empty. A pow: network has to say which block and how "
            "much work it means; the tag alone names only the chain."
        )

    message = NetworkFormal()
    try:
        message.ParseFromString(formal)
        document = {key: value.decode("utf-8") for key, value in message.entries.items()
                    if key in _KNOWN_KEYS}
    except (DecodeError, UnicodeDecodeError) as e:
        raise PowFormalError(f"Network.formal is not a valid protobuf map: {e}") from None
    extensions = {key: bytes(value) for key, value in message.entries.items()
                  if key not in _KNOWN_KEYS}

    missing = [key for key in _REQUIRED_KEYS if key not in document]
    if missing:
        raise PowFormalError(f"Network.formal is missing: {', '.join(missing)}.")

    version = _as_int(document["v"], "v")
    if version != SUPPORTED_VERSION:
        raise PowFormalError(
            f"Network.formal version {version} is not supported by this node "
            f"(it reads v{SUPPORTED_VERSION}). Refused rather than guessed."
        )

    chain = document["chain"]
    if not chain.strip() or chain.strip() != chain.strip().lower():
        raise PowFormalError(
            f"Network.formal: 'chain' must be a lowercase non-empty value, got {chain!r}."
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

    block_id = document["block_id"].strip().lower()
    if not block_id or any(c not in "0123456789abcdef" for c in block_id):
        raise PowFormalError(
            f"Network.formal: 'block_id' is not hexadecimal: {document['block_id']!r}."
        )

    return PowRequirement(
        chain=chain,
        block_id=block_id,
        min_cumulative_difficulty=_as_int(
            document["min_cumulative_difficulty"], "min_cumulative_difficulty"
        ),
        min_height=(
            _as_int(document["min_height"], "min_height") if "min_height" in document else None
        ),
        max_tip_age_s=(
            _as_int(document["max_tip_age_s"], "max_tip_age_s")
            if "max_tip_age_s" in document
            else None
        ),
        version=version,
        extensions=extensions,
    )


def canonical_formal(requirement: PowRequirement) -> bytes:
    """Deterministic protobuf map, including all opaque extensions."""
    pairs: Dict[str, str] = {
        "v": str(requirement.version),
        "chain": requirement.chain,
        "block_id": requirement.block_id,
        "min_cumulative_difficulty": str(requirement.min_cumulative_difficulty),
    }
    if requirement.min_height is not None:
        pairs["min_height"] = str(requirement.min_height)
    if requirement.max_tip_age_s is not None:
        pairs["max_tip_age_s"] = str(requirement.max_tip_age_s)
    if set(requirement.extensions) & _KNOWN_KEYS:
        raise PowFormalError("Extensions must not override known PoW fields.")
    entries = dict(requirement.extensions)
    entries.update({key: value.encode("utf-8") for key, value in pairs.items()})
    return NetworkFormal(entries=entries).SerializeToString(deterministic=True)


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
    """What other celaut nodes answer for this domain, as URLs to try.

    Imported where it is used: it pulls in the database and the gRPC transport, which a
    node resolving a DNS network has no reason to be loading here.

    ``Instance.Uri`` carries an address and a port and no scheme, so what comes back is
    tried over ``http``. That is a real loss of fidelity -- a peer that found an
    https-only endpoint has just handed us one we will fail to read -- and the honest
    place to fix it is the peer's ``protocol_stack``, not a guess here. It costs one
    failed request per such endpoint and nothing else: the candidate is dropped by the
    same verification every other candidate goes through.
    """
    try:
        from src.manager.network_discovery import ask_peers
    except Exception as e:  # pragma: no cover - environment-dependent
        logger(f"[POW] peer endpoint source unavailable: {type(e).__name__}: {e}")
        return []
    return [f"http://{ip}:{port}" for ip, port in ask_peers(network)]


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


def _uri_for(url: str) -> Optional[Tuple[str, int]]:
    """``(ip, port)`` for a peer URL, or None when it cannot be pinned to one.

    The firewall writes rules against addresses, so a hostname has to be resolved
    here -- which re-imports every caveat of DNS resolution (no DNSSEC, IPv4 only,
    frozen at launch). Preferring the numeric form when the crawl recorded one is
    the mitigation available at this layer.
    """
    parsed = urlparse(url if "//" in url else f"//{url}")
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or _SCHEME_PORTS.get((parsed.scheme or "").lower()) or ERGO_DEFAULT_PORT
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as e:
        logger(f"[POW] cannot resolve {host}: {e}")
        return None
    for info in infos:
        return info[4][0], int(port)
    return None


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
    i_slot = 1  # Internal port usage is irrelevant for an externally-reached peer.
    peers: List[celaut.Instance] = []
    seen_addresses = set()

    for url in candidate_urls(network, pow_tag, ask_peers=ask_peers):
        if len(peers) >= limit:
            break
        if not ergo_peer_satisfies(url, requirement, timeout=timeout):
            continue
        address = _uri_for(url)
        if address is None:
            continue
        if address in seen_addresses:
            continue
        seen_addresses.add(address)
        peers.append(
            celaut.Instance(
                api=celaut.Service.Api(
                    slot=[celaut.Service.Api.Slot(
                        port=i_slot,
                        transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                        # Echoed from the requester's declaration, exactly as the DNS
                        # path does: nothing here observed the peer's protocol stack.
                        protocol_stack=network.protocol_stack,
                    )],
                    payment_contracts=[],
                ),
                uri_slot=[celaut.Instance.Uri_Slot(
                    internal_port=i_slot,
                    uri=[celaut.Instance.Uri(ip=address[0], port=address[1])],
                )],
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
