"""Endpoints the ledger holds for one communication domain.

A ``Service.Network`` names a domain that has to be turned into addresses somehow.
DNS does it with a name lookup; a ``pow:<chain>`` network cannot, because what it
asks for -- "peers whose main chain contains block B and carries at least D work" --
is not something a name resolves to (issue #78,
``docs/proposals/78-network-guarantees-and-pow.md``).

So the addresses are *published*, on the same reputation contract every other claim
in celaut is published on, as an ordinary reputation box:

====  ==========================================================================
R4    the network-endpoints type NFT (``ledgers.ergo.reputation.NETWORK_ENDPOINTS_TYPE_NFT_ID``)
R5    :func:`network_descriptor_digest` of the domain the box is about
R8    polarity: *these endpoints serve that domain*, or *these do not*
R9    ``{"uris": ["http://host:9053", ...]}``
====  ==========================================================================

which is the shape ``contracts/ergo/transaction.__build_proof_box`` already writes
for every opinion, and the shape its R9 already carries this node's own ``Peer`` in
(``_self_network_data``). Nothing new on-chain, a different R4.

**What this decides, and what it does not.** It decides *who is asked first*. It
never decides who qualifies: every endpoint that comes back here is put through the
same verification as one typed into ``config.yaml`` by hand, and a peer that does not
meet the requirement is dropped however much ERG stands behind the box that named it.
That asymmetry is the whole reason a published endpoint list is safe to read from
strangers -- the cost of a lie is a wasted HTTP request, not a firewall rule.

Reading only. Publishing a list is a wallet operation (a JVM, a signature, a box), it
is not something a service launch should be doing, and nothing here needs this node to
have published anything to read what others have.
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List

from src.reputation_system.contracts.ergo.utils import (
    box_register,
    decode_bool_register,
    decode_coll_byte_hex,
    ergo_tree_template_hash,
    explorer_api_url,
    iter_unspent_boxes_by_registers,
    proof_standing,
)
from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

#: Which type NFT marks a box as an endpoint list rather than an opinion about a node.
#: Unset means this node reads none of them: an endpoint list and a reputation opinion
#: live on the same contract and differ only by R4, so reading without the filter would
#: take every opinion ever published about anything as an address list.
TYPE_NFT_KEY = "ledgers.ergo.reputation.NETWORK_ENDPOINTS_TYPE_NFT_ID"

#: Endpoint lists for one domain are a handful of boxes, not a census of the contract:
#: the search is filtered on R4 *and* R5, so anything approaching this bound means the
#: filter did not apply and the scan is reading somebody else's boxes.
MAX_BOXES = 2_000
PAGE_SIZE = 100

#: How many endpoints one box may contribute. A published list is untrusted input that
#: turns into outbound HTTP requests during a launch, so its length is this node's
#: decision and not the publisher's.
MAX_URIS_PER_BOX = 32


def network_descriptor_digest(network) -> str:
    """A stable name for the domain a ``Service.Network`` declares: blake2b-256, hex.

    R5 has to be one value, and a domain is declared as ``tags`` plus ``formal``. So
    the two are rendered canonically and hashed -- blake2b at 32 bytes, the hash the
    identity path already uses (:func:`node_identity.canonical_peer_content_digest`),
    rather than a second hash family for a second purpose.

    **Tags are sorted** because their order carries no meaning (``Peer.SignatureScheme``
    in celaut.proto says so of every component), and two nodes that listed the same tags
    differently must reach the same digest or they are looking in different places for
    the same thing. **``formal`` is hashed as hex**, so that a newline inside it cannot
    be mistaken for the separator between fields.

    **``prose`` is excluded, deliberately.** It is human text, ``same_component`` never
    compares it, and folding it in would give one domain a new name every time somebody
    reworded a sentence -- silently, since the only symptom is an empty answer.

    Note what this does *not* do: it is an exact digest, so it finds boxes published for
    exactly this ask. A publisher who covers a family of asks publishes the descriptor
    whose ``formal`` is empty, which is the descriptor that says "any ``pow:ergo``".
    """
    rendered = "\n".join(
        [f"tag={tag}" for tag in sorted(network.tags)]
        + [f"formal={bytes(network.formal).hex()}"]
    )
    return hashlib.blake2b(rendered.encode("utf-8"), digest_size=32).hexdigest()


def _uris_from_r9(box: dict) -> List[str]:
    """The endpoints one box publishes, or nothing when it does not publish any.

    Every failure here is "this box is not an endpoint list", never an exception: the
    contract is open, anybody can write anything into R9, and one unreadable box must
    not be able to stop a launch that has other boxes to read.
    """
    payload = decode_coll_byte_hex(str(box_register(box, "R9") or "") or "")
    if not payload:
        return []
    try:
        document = json.loads(bytes.fromhex(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(document, dict):
        return []

    uris = document.get("uris")
    if not isinstance(uris, list):
        return []

    return [
        entry.strip()
        for entry in uris[:MAX_URIS_PER_BOX]
        if isinstance(entry, str) and entry.strip()
    ]


def _backing(api_url: str, box: dict) -> float:
    """nanoERG of unrecoverable cost standing behind this box's claim.

    ``opinions.Opinion.backed_nanoerg``, computed here because these boxes are not
    opinions about a node and do not go through that reader: the box's share of what
    its proof has *assigned*, times what the proof has burned. The share rather than
    the raw token count, because a token supply is chosen by whoever mints it and
    minting more of them costs nothing.

    Zero when the proof cannot be read -- the claim is still real, we just cannot price
    it, and inventing a denominator would overstate it.
    """
    assets = box.get("assets") or []
    if not assets:
        return 0.0
    token_id = str(assets[0].get("tokenId") or "")
    try:
        amount = int(assets[0].get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    if not token_id or amount <= 0:
        return 0.0

    try:
        standing = proof_standing(api_url, token_id)
    except Exception as e:  # explorer failures are priced at zero, not raised
        logger(f"[NETWORK-ENDPOINTS] could not price proof {token_id}: {type(e).__name__}: {e}")
        return 0.0

    if not standing.assigned_amount:
        return 0.0
    return (amount / standing.assigned_amount) * standing.burned_nanoerg


def endpoints_for(network) -> List[str]:
    """Endpoints published on the ledger for ``network``, best-backed first.

    A box staking *for* the domain offers its endpoints; one staking against (R8 false)
    argues the same endpoints do not serve it. Both are read, each endpoint is scored as
    the backing for it minus the backing against it, and an endpoint is returned when
    somebody offered it and nothing outweighs them -- so withdrawing an endpoint is
    something the network can do, and a publisher cannot make one unrejectable by
    repeating it.

    Returns ``[]`` rather than raising for every failure, including an unreachable
    explorer: this is one candidate source among several (``config.yaml`` names
    endpoints by hand, and the chain crawl finds more), and a launch must not depend on
    an explorer being up. The empty answer is logged with its reason so it is
    distinguishable from "nobody has published anything for this domain".
    """
    type_nft = str(ConfigManager().get(TYPE_NFT_KEY, "") or "").strip().lower()
    if not type_nft:
        logger(
            f"[NETWORK-ENDPOINTS] {TYPE_NFT_KEY} is unset, so no endpoint list is read "
            "from the ledger: without it an ordinary reputation opinion would be taken "
            "for an address list."
        )
        return []

    digest = network_descriptor_digest(network)

    try:
        api_url = explorer_api_url()
        template_hash = ergo_tree_template_hash(REPUTATION_PROOF_ERGO_TREE)
        boxes = list(
            iter_unspent_boxes_by_registers(
                api_url,
                template_hash,
                {"R4": type_nft, "R5": digest},
                page_size=PAGE_SIZE,
                max_boxes=MAX_BOXES,
            )
        )
    except Exception as e:
        logger(
            f"[NETWORK-ENDPOINTS] could not read endpoint lists for {digest}: "
            f"{type(e).__name__}: {e}"
        )
        return []

    # The template hash identifies the contract's *code*, not the exact tree, so what
    # comes back still has to be checked against the tree this node reads -- the same
    # check `contracts/ergo/opinions` makes on the boxes it collects.
    scores: Dict[str, float] = {}
    offered_by: Dict[str, int] = {}
    order: List[str] = []
    for box in boxes:
        if str(box.get("ergoTree") or "").lower() != REPUTATION_PROOF_ERGO_TREE.lower():
            continue
        uris = _uris_from_r9(box)
        if not uris:
            continue

        # A box that declares no polarity is skipped rather than read either way, as
        # `contracts/ergo/opinions` skips it: reading "no direction" as *for* would
        # invent an endorsement nobody published, and as *against* a hostile one.
        positive = decode_bool_register(str(box_register(box, "R8") or ""))
        if positive is None:
            logger(
                f"[NETWORK-ENDPOINTS] box {box.get('boxId')} declares no polarity in R8; "
                "skipped."
            )
            continue

        signed = _backing(api_url, box) * (1 if positive else -1)
        for uri in dict.fromkeys(uris):
            if uri not in scores:
                scores[uri] = 0.0
                offered_by[uri] = 0
                order.append(uri)
            scores[uri] += signed
            offered_by[uri] += 1 if positive else 0

    # Offered when somebody published it and nothing outweighs them. The count and the
    # score are separate questions on purpose: a box whose proof could not be priced
    # backs its endpoint with 0, and dropping it for that would let an explorer hiccup
    # silently empty the list, while a *negative* box with real ERG behind it still
    # takes an endpoint out.
    offered = [uri for uri in order if offered_by[uri] and scores[uri] >= 0]
    offered.sort(key=lambda uri: scores[uri], reverse=True)

    logger(
        f"[NETWORK-ENDPOINTS] {len(offered)} endpoint(s) published for {digest} "
        f"across {len(boxes)} box(es)."
    )
    return offered
