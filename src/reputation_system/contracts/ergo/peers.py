"""Peers that announce themselves on the Ergo reputation contract (issue #494).

A node that publishes its reputation proof also writes a box that declares itself
(``transaction._self_network_data``): R4 is the CELAUT node type, R5 is the identity
public key of the node, and R9 is its signed ``Peer``, with the addresses. This module
reads those boxes. It is the "list peers" side of the Ergo reader that
``opinions.py`` has for the "read opinions" side.

What it returns is a *claim*. The ``Peer`` is returned as the node signed it, and the
caller registers it through ``manager.add_peer_instance``, which verifies the signature
and that the key is the identity. This module checks only what it needs to pick the boxes
(that the register R5 and the key in R9 are the same, and the ERG behind the proof).

Minting a proof costs nothing, so the ERG in the proof is the filter: the proof must have
at least ``ledgers.ergo.reputation.DISCOVERY_MIN_LOCKED_ERG`` locked behind its opinions.
Only its boxes at the contract count, and only the share of its tokens that is not in a
self-opinion (see :func:`utils.proof_standing`). That ERG cannot be taken out.

**One peer per proof.** A proof gives only its newest declaration. Without this, one proof
with enough ERG could declare any number of keys and fill every slot of a new node.

**A copy does not hide the original.** Anyone can copy the signed ``Peer`` of a node into
a box of another proof. A key is kept with every proof that declares it, and it is taken
when one of those proofs has enough ERG.

The result is at most ``limit`` peers, in a random order: with a fixed order, every new
node would pick the same peers.

Explorer reads only. Every request has the deadline of ``ledger_http`` (30 s), and the
whole read stops at ``deadline``.
"""
import json
import random
import time
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from google.protobuf import json_format

from protos import celaut_pb2
from src.reputation_system.contracts.ergo.opinions import _opinion_boxes
from src.reputation_system.contracts.ergo.utils import (
    box_register,
    decode_coll_byte_hex,
    explorer_api_url,
    proof_standing,
)
from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE
from src.utils.config import ConfigManager
from src.utils.keyvalue import json_objects_to_entries
from src.utils.logger import LOGGER as logger

NANOERG_PER_ERG = 10 ** 9
DEFAULT_MIN_LOCKED_ERG = "0.1"
MIN_LOCKED_KEY = "ledgers.ergo.reputation.DISCOVERY_MIN_LOCKED_ERG"

# An Ergo register holds at most 4 KB. A bigger value is not a box that this node wrote.
MAX_ADVERTISEMENT_BYTES = 4096
# A `Peer` as JSON is a few levels deep. A deeper document is not one, and parsing it
# could raise RecursionError.
MAX_JSON_DEPTH = 32


def _json_depth(text: str) -> int:
    """The deepest nesting of objects and arrays in ``text``, without parsing it."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def min_locked_nanoerg() -> int:
    """The minimum ERG locked in a proof, in nanoERG. A bad value gives the default."""
    raw = ConfigManager().get(MIN_LOCKED_KEY, DEFAULT_MIN_LOCKED_ERG)
    try:
        value = Decimal(str(raw).strip())
        if not value.is_finite() or value < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        logger(f"{MIN_LOCKED_KEY} is {raw!r}, not a number of ERG; using {DEFAULT_MIN_LOCKED_ERG}.")
        value = Decimal(DEFAULT_MIN_LOCKED_ERG)
    return int(value * NANOERG_PER_ERG)


def peer_of_box(box: dict, node_type_nft: str) -> Optional[Tuple[celaut_pb2.Peer, str]]:
    """``(peer, proof id)`` when ``box`` is the box that declares a node, else None.

    Not the box of an opinion about somebody else: those carry other data in R9, and R5
    is the key of the node they are about. Here the key in R5 must be the key that the
    ``Peer`` in R9 names. The signature is not checked: that is the job of the caller.
    """
    # Required: the explorer always gives it, and a box at another address is not a box
    # of the reputation contract.
    ergo_tree = str(box.get("ergoTree") or "")
    if ergo_tree.lower() != REPUTATION_PROOF_ERGO_TREE.lower():
        return None
    r4 = (decode_coll_byte_hex(str(box_register(box, "R4") or "")) or "").lower()
    if r4 != node_type_nft.lower():
        return None
    assets = box.get("assets") or []
    proof_id = (assets[0].get("tokenId") or "").lower() if assets else ""
    if not proof_id:
        return None
    r5 = (decode_coll_byte_hex(str(box_register(box, "R5") or "")) or "").lower()
    r9 = decode_coll_byte_hex(str(box_register(box, "R9") or ""))
    if not r5 or not r9 or len(r9) > 2 * MAX_ADVERTISEMENT_BYTES:
        return None
    try:
        text = bytes.fromhex(r9).decode("utf-8")
        if not text.lstrip().startswith("{") or _json_depth(text) > MAX_JSON_DEPTH:
            return None
        document = json.loads(text)
        if not isinstance(document, dict):
            return None
        peer = json_format.ParseDict(
            json_objects_to_entries(document, celaut_pb2.Peer.DESCRIPTOR),
            celaut_pb2.Peer(),
        )
    except Exception:
        # Includes "No IP available.": the text of a box with no address to announce.
        # Any error is one bad box: anybody can write a box at the contract, so no box
        # may stop the read of the others.
        return None
    if not peer.public_key or peer.public_key.lower() != r5:
        return None
    return peer, proof_id


def well_reputed_peers(
    limit: int,
    deadline: float,
    exclude: FrozenSet[str] = frozenset(),
    accept: Optional[Callable[[celaut_pb2.Peer], bool]] = None,
) -> list:
    """At most ``limit`` signed ``Peer`` claims from proofs with enough ERG locked.

    ``deadline`` is a ``time.monotonic()`` value. When it passes, the read stops and
    returns the peers that it has. ``exclude`` holds peer ids that the node has already
    (and its own key); they cost no request. ``accept`` is the caller's local check of a
    claim (expiry, addresses). A claim that it refuses costs no request and takes no
    slot. Raises when the explorer cannot be read at all, so the caller can say why: an
    empty list means that the chain has no such peer.
    """
    if limit <= 0:
        return []
    node_type_nft = str(
        ConfigManager().get("ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID") or ""
    )
    if not node_type_nft:
        raise ValueError(
            "ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID is unset: there is no way "
            "to tell which boxes declare a node."
        )
    minimum = min_locked_nanoerg()
    api_url = explorer_api_url()
    exclude = frozenset(key.lower() for key in exclude)

    # One peer per proof: its newest declaration, since a node that publishes again
    # leaves older boxes.
    by_proof: Dict[str, celaut_pb2.Peer] = {}
    for box in _opinion_boxes(api_url, {"R4": node_type_nft.lower()}):
        if time.monotonic() >= deadline:
            logger("Ledger peer discovery: out of time while reading boxes.")
            break
        try:
            found = peer_of_box(box, node_type_nft)
            if found is None:
                continue
            peer, proof_id = found
            if peer.public_key.lower() in exclude:
                continue
            if accept is not None and not accept(peer):
                continue
        except Exception as e:
            logger(f"Ledger peer discovery: skipped a box that cannot be read: {e}")
            continue
        known = by_proof.get(proof_id)
        if known is None or peer.ts > known.ts:
            by_proof[proof_id] = peer

    # Every proof that declares a key, newest declaration first.
    by_key: Dict[str, List[Tuple[celaut_pb2.Peer, str]]] = {}
    for proof_id, peer in by_proof.items():
        by_key.setdefault(peer.public_key.lower(), []).append((peer, proof_id))
    for claims in by_key.values():
        claims.sort(key=lambda claim: claim[0].ts, reverse=True)

    keys = list(by_key)
    random.shuffle(keys)
    chosen = []
    standings: Dict[str, int] = {}
    out_of_time = False
    for key in keys:
        if len(chosen) >= limit or out_of_time:
            break
        for peer, proof_id in by_key[key]:
            if time.monotonic() >= deadline:
                logger("Ledger peer discovery: out of time while reading proofs.")
                out_of_time = True
                break
            if proof_id not in standings:
                try:
                    standings[proof_id] = proof_standing(
                        api_url, proof_id, deadline=deadline
                    ).burned_nanoerg
                except Exception as e:
                    logger(f"Could not read the ERG locked in proof {proof_id}: {e}")
                    standings[proof_id] = -1
            if standings[proof_id] >= minimum:
                chosen.append(peer)
                break
    logger(
        f"Ledger peer discovery: {len(by_key)} node(s) declared on the Ergo contract by "
        f"{len(by_proof)} proof(s), {len(chosen)} with at least {minimum} nanoERG locked."
    )
    return chosen
