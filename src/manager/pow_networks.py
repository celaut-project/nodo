"""Proof-of-Work communication domains: ``pow:<chain>`` networks.

A ``Service.Network`` whose tag is ``pow:ergo`` names a *class* of domain -- the
Ergo PoW network -- and its ``formal`` field says which instance of that class is
meant: "peers whose main chain contains block B and carries at least D cumulative
work". Tags alone cannot express that, which is why this is the first network kind
in nodo that reads ``formal`` at all (see
``docs/proposals/78-network-guarantees-and-pow.md``, issue #78).

``formal`` is canonical UTF-8 JSON, not a proto message inside ``bytes``: the field
is authored by hand, packed from ``service.json``, and read by more than one
language, so a schema nobody outside this repository can see would be the wrong
trade. celaut-project/skills made the same choice for its Strict Definitions.

**Unknown keys are rejected rather than ignored.** A node that silently drops a
constraint it does not understand grants more than was asked for -- the same rule
``src/utils/network_policy.py`` states for a policy list it failed to read.

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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from protos import celaut_pb2 as celaut
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


def _as_int(value: Any, field: str) -> int:
    """A non-negative integer, accepting the decimal *string* form.

    Cumulative work does not fit in a JSON number (an IEEE double loses precision
    well below Ergo's current score), so it travels as a string and is compared as
    an exact integer. A bool is refused explicitly: in Python it would otherwise
    pass as an int and read ``true`` as 1.
    """
    if isinstance(value, bool):
        raise PowFormalError(f"Network.formal: '{field}' must be an integer, got a boolean.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise PowFormalError(f"Network.formal: '{field}' is empty.")
        try:
            parsed = int(text, 10)
        except ValueError:
            raise PowFormalError(
                f"Network.formal: '{field}' must be a base-10 integer, got {value!r}."
            ) from None
    else:
        raise PowFormalError(
            f"Network.formal: '{field}' must be an integer or a decimal string, got "
            f"{type(value).__name__}."
        )
    if parsed < 0:
        raise PowFormalError(f"Network.formal: '{field}' must not be negative, got {parsed}.")
    return parsed


def parse_pow_formal(formal: bytes, tag: Optional[str] = None) -> PowRequirement:
    """Read ``Network.formal`` as a :class:`PowRequirement`.

    ``tag`` is checked against the declared chain when given: a ``pow:ergo`` tag
    carrying ``"chain": "bitcoin"`` is a malformed specification, not a
    cross-chain request, and reading it either way would mean resolving one chain
    for a tag the operator's policy vetted as another.
    """
    if not formal:
        raise PowFormalError(
            "Network.formal is empty. A pow: network has to say which block and how "
            "much work it means; the tag alone names only the chain."
        )

    try:
        document = json.loads(bytes(formal).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PowFormalError(f"Network.formal is not UTF-8 JSON: {e}") from None

    if not isinstance(document, dict):
        raise PowFormalError(
            f"Network.formal must be a JSON object, got {type(document).__name__}."
        )

    unknown = sorted(set(document) - _KNOWN_KEYS)
    if unknown:
        raise PowFormalError(
            f"Network.formal has unknown key(s): {', '.join(unknown)}. They are refused "
            "rather than ignored, because a constraint this node drops silently is a "
            f"constraint nobody applied. Known keys: {', '.join(sorted(_KNOWN_KEYS))}."
        )

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
    if not isinstance(chain, str) or not chain.strip() or chain.strip() != chain.strip().lower():
        raise PowFormalError(
            f"Network.formal: 'chain' must be a lowercase non-empty string, got {chain!r}."
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

    block_id = document["block_id"]
    if not isinstance(block_id, str):
        raise PowFormalError(
            f"Network.formal: 'block_id' must be a hex string, got {type(block_id).__name__}."
        )
    block_id = block_id.strip().lower()
    if not block_id or any(c not in "0123456789abcdef" for c in block_id):
        raise PowFormalError(f"Network.formal: 'block_id' is not hexadecimal: {document['block_id']!r}.")

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
    )


def canonical_formal(requirement: PowRequirement) -> bytes:
    """The requirement back as canonical JSON: sorted keys, no padding.

    Canonicalisation exists so a ``formal`` can be hashed or logged reproducibly,
    never as a validity condition -- :func:`parse_pow_formal` accepts any equivalent
    encoding.
    """
    document: Dict[str, Any] = {
        "v": requirement.version,
        "chain": requirement.chain,
        "block_id": requirement.block_id,
        # A string, for the same reason it is read as one: the value outgrows a
        # double, and a reader that treats JSON numbers as doubles would round it.
        "min_cumulative_difficulty": str(requirement.min_cumulative_difficulty),
    }
    if requirement.min_height is not None:
        document["min_height"] = requirement.min_height
    if requirement.max_tip_age_s is not None:
        document["max_tip_age_s"] = requirement.max_tip_age_s
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def ergo_candidate_urls() -> List[str]:
    """Candidate Ergo REST endpoints, most trusted first, de-duplicated.

    Three sources, and the order is the trust order rather than a preference:

    1. ``ledgers.ergo.NODE_URL`` -- the node the operator configured, which this
       node already trusts with reputation reads and payment proofs.
    2. ``pow_networks.EXTRA_PEERS`` -- an explicit operator list, for somebody
       running their own.
    3. ``ledgers.ergo.HTTP_PEERS_PATH`` -- the crawl in ``src/manager/ergo.py``.
       Strangers. The file is **read**, never refreshed from here: the crawl
       recurses unboundedly and a service launch is not the place to start one.

    Every one of them is verified the same way regardless of where it came from;
    the ordering only decides who is asked first.
    """
    urls: List[str] = []

    configured = str(env_manager.get("ledgers.ergo.NODE_URL", "") or "").strip()
    if configured:
        urls.append(configured)

    extra = env_manager.get(f"{CONFIG_BLOCK}.EXTRA_PEERS", []) or []
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, (list, tuple)):
        urls.extend(str(entry).strip() for entry in extra if str(entry).strip())

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
) -> List[celaut.Instance]:
    """Peers that satisfy a ``pow:<chain>`` network, as at most **one** Instance.

    One Instance carrying N uris, and not N Instances, because of how the rules are
    written: ``configure_guest_firewall_policy`` stops at the first peer instance a
    rule could be applied for, while ``allow_connection_to_instance`` walks *every*
    uri of the instance it is given. N instances would open the first peer and
    silently drop the rest. ``resolve_domain`` already returns multiple A records
    this way.

    No qualifying peer returns ``[]`` rather than raising: "nobody meets D right
    now" is a statement about the world and a transient one, unlike a policy
    rejection (intent) or an unreadable ancestor spec (this node's integrity), both
    of which do abort. The reasoning is in the proposal document.
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
    uris: List[celaut.Instance.Uri] = []
    seen_addresses = set()

    for url in ergo_candidate_urls():
        if len(uris) >= limit:
            break
        if not ergo_peer_satisfies(url, requirement, timeout=timeout):
            continue
        address = _uri_for(url)
        if address is None:
            continue
        if address in seen_addresses:
            continue
        seen_addresses.add(address)
        uris.append(celaut.Instance.Uri(ip=address[0], port=address[1]))

    if not uris:
        # Distinguishable in the log from the wildcard's empty answer and from an
        # AAAA-only DNS name, which are the other two ways `[]` is reached today.
        logger(
            f"[POW] no peer satisfies {pow_tag} (block {requirement.block_id}, "
            f"work >= {requirement.min_cumulative_difficulty}); resolving to no peers."
        )
        return []

    i_slot = 1  # Internal port usage is irrelevant for an externally-reached peer.
    return [
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
            uri_slot=[celaut.Instance.Uri_Slot(internal_port=i_slot, uri=uris)],
        )
    ]
