"""Every opinion the Ergo chain holds about one node.

Every proof in the ecosystem lives on one reputation contract, so "what does the
network think of this node" is a filter over that contract: a box is an opinion about us
when R4 is the CELAUT node type and R5 is our identity public key -- the same two
registers ``transaction.__create_reputation_proof_tx`` writes when *we* publish an
opinion about somebody else.

The explorer applies that filter itself, so the usual case is one request rather than a
page per hundred boxes on a contract that grows with the whole ecosystem. Scanning the
contract address remains as a fallback, because the search needs a derived template
hash and the endpoint has changed shape before -- see :func:`_opinion_boxes`.

What the box stakes is read against the supply the publishing proof has actually
assigned to opinions -- everything it holds except the reserve in its self-pointing box
-- because a share is the only comparable quantity and the reserve is not a judgement
about anything. See :func:`proof_standing` for why the minted supply is the wrong
denominator, and the module docstring of :mod:`src.reputation_system.opinions` for what
that costs when it is used.

Explorer reads only -- no wallet, no JVM, no transaction. It answers the same question
for any node id, ours or a peer's, since nothing here depends on holding a key.
"""

from typing import Dict, Iterator, List, Optional

import requests

from src.reputation_system.contracts.ergo.utils import (
    ProofStanding,
    box_register,
    decode_bool_register,
    decode_coll_byte_hex,
    ergo_tree_template_hash,
    explorer_api_url,
    iter_unspent_boxes_by_address,
    iter_unspent_boxes_by_registers,
    proof_standing,
)
from src.reputation_system.envs import (
    LEDGER,
    REPUTATION_PROOF_ADDRESS,
    REPUTATION_PROOF_ERGO_TREE,
)
from src.reputation_system.opinions import Opinion
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

# The reputation contract holds every proof anyone has ever published, and we want the
# handful of boxes that name one node. Deliberately larger than the 2000 the ownership
# lookup settles for: that one breaks on its first match, this one has to see them all,
# and a node missing from its own reputation page because the scan stopped early is a
# wrong answer rather than a slow one. It is the *fallback* that needs the headroom;
# the filtered search returns a box or two.
MAX_BOXES = 20_000
PAGE_SIZE = 100


def _explorer_json(url: str) -> Optional[dict]:
    """``url``'s JSON body, or None with a log line when it cannot be had.

    None rather than an exception because every caller here is enriching an opinion it
    has already found: a proof whose supply or block we cannot read is still an opinion
    published about this node, and dropping it would understate the node's reputation.
    """
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as e:
        logger(f"Reputation read failed for {url}: {e}")
        return None
    if response.status_code != 200:
        logger(f"Reputation read failed for {url}: HTTP {response.status_code}")
        return None
    try:
        return response.json()
    except ValueError as e:
        logger(f"Unreadable reputation response from {url}: {e}")
        return None


class _Chain:
    """The explorer lookups an opinion needs, each asked once per distinct id.

    A page of boxes routinely repeats a proof and a block, and both lookups are a
    round-trip; memoising them is what keeps a node with a dozen opinions to a handful
    of requests rather than two per box.
    """

    def __init__(self, api_url: str):
        self.api_url = api_url.rstrip("/")
        self._standings: Dict[str, ProofStanding] = {}
        self._timestamps: Dict[str, Optional[int]] = {}

    def standing(self, token_id: str) -> ProofStanding:
        """What the proof has assigned and burned — the two figures a share needs.

        One request per proof for both, and zeros when it cannot be read: an opinion
        whose denominator is unknown reports a zero share rather than a share invented
        from a guessed denominator, and no sacrifice rather than one it may not have
        made. Both err towards understating the proof.
        """
        if token_id not in self._standings:
            try:
                self._standings[token_id] = proof_standing(self.api_url, token_id)
            except Exception as e:
                logger(f"Could not read what proof {token_id} has assigned: {e}")
                self._standings[token_id] = ProofStanding(0, 0, 0)
        return self._standings[token_id]

    def block_time(self, block_id: str) -> Optional[int]:
        """When the block carrying a box was mined, in unix **seconds**.

        The explorer reports header timestamps in milliseconds; they are divided down
        here so every date in this module is in the one unit.
        """
        if block_id not in self._timestamps:
            payload = _explorer_json(f"{self.api_url}/api/v1/blocks/{block_id}") or {}
            header = (payload.get("block") or {}).get("header") or {}
            millis = header.get("timestamp")
            self._timestamps[block_id] = int(millis) // 1000 if millis else None
        return self._timestamps[block_id]


def _is_opinion_about(box: dict, node_type_nft: str, node_id: str) -> bool:
    """Whether ``box`` is an opinion of the CELAUT node type addressed to ``node_id``.

    Applied even when the explorer has already filtered, because what it filters on is
    weaker than what we need. A search is keyed by the ErgoTree *template*, which
    constant segregation makes shared by every contract with the same code and different
    constants: at the time of writing the template matched 311 unspent boxes where the
    canonical contract address held 307, and a search for the node type returned four
    boxes of which one was on such a look-alike tree. A box like that is invisible to
    every other reader of the chain, so counting it would credit this node with
    reputation nobody else can see.
    """
    ergo_tree = box.get("ergoTree")
    if ergo_tree and ergo_tree.lower() != REPUTATION_PROOF_ERGO_TREE.lower():
        return False
    pointer = (decode_coll_byte_hex(str(box_register(box, "R5") or "")) or "").lower()
    if pointer != node_id.lower():
        return False

    # A box pointing at its own token id is the proof's reserve -- how it declares
    # itself and parks the supply it has not assigned to anything (see
    # `proof_standing`). It is not an opinion about a node, and counting it as one
    # would read a proof's own unspent supply as reputation held on somebody. It only
    # reaches here at all through the boxes old nodo minted with the proof id in R5
    # instead of the target's identity key (see the R5 note in `transaction.py`).
    assets = box.get("assets") or []
    token_id = (assets[0].get("tokenId") or "").lower() if assets else ""
    if token_id and pointer == token_id:
        return False
    if node_type_nft:
        r4 = (decode_coll_byte_hex(str(box_register(box, "R4") or "")) or "").lower()
        if r4 != node_type_nft.lower():
            return False
    return True


def _opinion(box: dict, chain: _Chain) -> Optional[Opinion]:
    """``box`` as an :class:`Opinion`, or None when it is not one.

    Two ways not to be: no reputation token, so there is no stake, and no polarity in
    R8, so there is no direction. Either way the box is on the contract and addressed
    to this node but says nothing countable, and a report that guessed at the missing
    half would be reporting a stake nobody took.
    """
    assets = box.get("assets") or []
    proof_id = assets[0].get("tokenId") if assets else None
    if not proof_id:
        return None

    polarity = decode_bool_register(str(box_register(box, "R8") or ""))
    if polarity is None:
        logger(f"Reputation box {box.get('boxId')} declares no polarity in R8; skipped.")
        return None

    block_id = box.get("blockId")
    standing = chain.standing(proof_id)
    return Opinion(
        ledger=LEDGER,
        proof_id=proof_id,
        owner=decode_coll_byte_hex(str(box_register(box, "R7") or "")) or "",
        amount=int(assets[0].get("amount") or 0),
        assigned_amount=standing.assigned_amount,
        # R8 is the declared polarity, and a box that declares none is not counted
        # either way (see :func:`decode_bool_register`) -- which is why such a box is
        # dropped by :func:`_opinion` rather than read as a stake of some sign.
        positive=polarity,
        published_at=chain.block_time(block_id) if block_id else None,
        box_id=str(box.get("boxId") or ""),
        burned_nanoerg=standing.burned_nanoerg,
    )


def _opinion_boxes(api_url: str, registers: Dict[str, str]) -> Iterator[dict]:
    """The boxes worth looking at, filtered by the explorer wherever it can be.

    The register-filtered search turns the whole reputation contract into the one or two
    boxes that name this node, which is the difference between one request and a page
    per hundred boxes on a contract shared by every proof in the ecosystem.

    It is a fast path and not the only path. It needs the contract's ErgoTree *template*
    hash, derived from the pinned tree, so it stops being available if that tree ever
    gains a constant type :func:`ergo_tree_template` cannot measure; and the endpoint's
    own contract has moved before -- an omitted ``ergoTreeTemplateHash`` used to be
    accepted and is now rejected outright. Neither may be allowed to turn into "no
    opinions", which is why a search that cannot be used at all falls back to scanning
    the contract address, and both paths run through the same client-side filter.

    The fallback is only ever taken *before the first box is emitted*. Once the search
    has handed anything back, a failure part-way through is raised: restarting on the
    address scan would re-emit what has already been yielded, and the caller adds these
    up, so the node's own reputation would come out inflated. Half an answer has to
    read as an error, never as a figure.
    """
    emitted = False
    try:
        template_hash = ergo_tree_template_hash(REPUTATION_PROOF_ERGO_TREE)
        for box in iter_unspent_boxes_by_registers(
            api_url, template_hash, registers, page_size=PAGE_SIZE, max_boxes=MAX_BOXES
        ):
            emitted = True
            yield box
        return
    except Exception as e:
        if emitted:
            raise
        logger(f"Register-filtered reputation search unavailable ({e}); scanning the contract.")

    yield from iter_unspent_boxes_by_address(
        api_url, REPUTATION_PROOF_ADDRESS, page_size=PAGE_SIZE, max_boxes=MAX_BOXES
    )


def opinions_about(node_id: str) -> List[Opinion]:
    """Every unspent opinion on the Ergo chain addressed to ``node_id``.

    Raises when the explorer cannot be reached: an empty list means "the network holds
    no opinion about this node", and a failed read must not be able to say that.
    """
    if not node_id:
        raise ValueError("No node id to look up reputation for.")

    node_type_nft = str(
        ConfigManager().get("ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID") or ""
    )
    if not node_type_nft:
        logger(
            "ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID is unset, so opinions of "
            "every type addressed to this node are counted."
        )

    # Rendered values -- the raw payload hex, no type tag. The endpoint matches those
    # and returns nothing at all for a serialized `0e…` value, so writing the filter
    # the other way would read as "nobody has an opinion about this node".
    registers = {"R5": node_id.lower()}
    if node_type_nft:
        registers["R4"] = node_type_nft.lower()

    chain = _Chain(explorer_api_url())
    opinions = [
        opinion
        for box in _opinion_boxes(chain.api_url, registers)
        if _is_opinion_about(box, node_type_nft, node_id)
        for opinion in [_opinion(box, chain)]
        if opinion
    ]
    logger(f"Found {len(opinions)} on-chain opinions about {node_id}.")
    return opinions
