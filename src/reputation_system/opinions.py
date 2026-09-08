"""What the network says about a node, as the opinions that say it.

A node's reputation is not a number somebody keeps for it. It is a set of *opinions*,
each one a stake: a reputation proof holds a fixed supply of tokens, and whoever owns
that proof decides how much of it to put behind each thing it has an opinion about. So
an opinion is worth the **share of its proof** it commits, not the raw token count --
one token out of ten committed is a tenth of what that proof has to say, and one out of
a thousand is a thousandth of it.

The share it is a share *of* is the supply the proof has actually **assigned to
opinions**, not everything it minted. A proof keeps its unassigned tokens in a box
pointing at itself (R5 = its own token id), which is a reserve and not a judgement about
anything, and in practice that reserve is nearly all of it: measured on mainnet, every
live profile holds ~99,999,9xx of its 99,999,999 tokens in one self-pointing box and
spends **one token per opinion**. Against the minted supply every real opinion in the
system is therefore 0.000001% -- a denominator that makes the whole question
unanswerable. Against the assigned supply, one token out of the ninety-five a proof has
deployed is 1.05%, which is what it means.

This is where :class:`Opinion.weight` parts from ``ReputationProof.compute`` in
reputation-systems/reputation-system, which divides by the token's ``emissionAmount``.
Same numerator, a denominator that excludes the reserve.

This module holds the shape and the arithmetic; a ledger module produces the opinions
(for Ergo, :mod:`src.reputation_system.contracts.ergo.opinions`). Nothing here talks to
a chain, so the totals can be tested without one.

The polarity is a register on the box, not the sign of the weight: a proof can stake a
tenth of itself *against* a node just as deliberately as for it. Positives and negatives
are therefore reported apart, and netted only where a single figure is asked for --
"+0.4 and -0.3" and "+0.1" are different things to know about a node.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Opinion:
    """One reputation box, read as the opinion it publishes about a node.

    ``amount`` out of ``assigned_amount`` is the stake; ``positive`` is R8, the polarity
    the box declares.

    ``published_at`` is when the box was created, and it is the age of *this box*, not
    of the opinion: revising a reputation box spends it and writes a new one, so the
    chain keeps no earlier date. It is reported per opinion and never aggregated into
    "reputation earned in the last week", because that number cannot be built from it.
    nodo re-splits its whole supply on every submission (``submit_to_ledger``) and
    spends every one of its boxes to do it, so one peer reaching
    ``LEDGER_REPUTATION_SUBMISSION_THRESHOLD`` events re-dates every opinion that node
    holds -- ours included. A window over these dates would measure how often the
    publisher republishes, which is not what anybody is asking. Reputation is therefore
    reported as a standing and as what backs it; only money is reported as a flow.
    """

    ledger: str
    #: Token id of the proof holding this opinion — its publisher's on-chain identity.
    proof_id: str
    #: R7, the owner's ``propositionBytes``: the wallet that can spend the box.
    owner: str
    #: Reputation tokens this box stakes on the node.
    amount: int
    #: The publishing proof's tokens that are actually saying something: its supply
    #: minus what it holds in reserve in self-pointing boxes. See the module docstring
    #: for why the minted supply is the wrong denominator.
    assigned_amount: int
    #: R8. False is an opinion against the node, not the absence of one.
    positive: bool
    #: Unix seconds, or None when the block carrying the box could not be dated.
    published_at: Optional[int]
    box_id: str
    #: Total nanoERG irrecoverably sunk into the publishing proof, across all of its
    #: unspent boxes -- ``total_burned`` in reputation-systems/reputation-system. The
    #: floor is the min-box value each of its boxes needs to exist, so a proof sitting
    #: at 0.001 ERG has had nothing sacrificed into it.
    burned_nanoerg: int = 0

    @property
    def weight(self) -> float:
        """Share of what the publishing proof has assigned to opinions, in ``0..1``.

        Zero for a proof whose assigned supply could not be read rather than an error:
        the opinion is real and is still worth listing, we just cannot say what fraction
        of its publisher's voice it is, and inventing a denominator would overstate it.
        """
        return self.amount / self.assigned_amount if self.assigned_amount else 0.0

    @property
    def signed_weight(self) -> float:
        return self.weight if self.positive else -self.weight

    @property
    def backed_nanoerg(self) -> float:
        """The publishing proof's sunk cost that stands behind *this* opinion.

        ``weight * burned_nanoerg``: a proof that sacrificed 10 ERG and commits half of
        itself here puts 5 ERG of unrecoverable value behind this node. Multiplying the
        *share* rather than the raw token count is what makes it comparable between
        proofs -- token supplies are chosen by whoever mints them, so
        ``token_amount * burned`` (as the reference web app's profile score computes it)
        rewards minting a larger supply, which costs nothing.
        """
        return self.weight * self.burned_nanoerg


@dataclass(frozen=True)
class ReputationTotals:
    """Opinions added up, with for and against kept apart.

    Two currencies, because they answer different questions: the shares say how much of
    each proof is committed, and the backing says how much unrecoverable ERG stands
    behind that commitment. A node can have a large share of proofs that cost nothing to
    create, which is what a share-only figure cannot tell you.
    """

    positive: float
    negative: float
    positive_proofs: int
    negative_proofs: int
    #: nanoERG of sunk cost behind the opinions in each direction.
    positive_backing: float = 0.0
    negative_backing: float = 0.0

    @property
    def net(self) -> float:
        return self.positive - self.negative

    @property
    def net_backing(self) -> float:
        return self.positive_backing - self.negative_backing

    @property
    def proofs(self) -> int:
        return self.positive_proofs + self.negative_proofs


def by_proof(opinions: Iterable[Opinion]) -> Dict[str, float]:
    """Each proof's verdict on the node, in ``-1..1``, keyed by proof id.

    One proof may hold several boxes about the same node, so its verdict is the sum of
    their signed weights -- which is exactly ``ReputationProof.compute`` restricted to
    one target. Summing per proof before summing across proofs is what stops a proof
    that has split its stake into ten boxes from counting ten times.
    """
    verdicts: Dict[str, float] = {}
    for opinion in opinions:
        verdicts[opinion.proof_id] = verdicts.get(opinion.proof_id, 0.0) + opinion.signed_weight
    return verdicts


def burned_by_proof(opinions: Iterable[Opinion]) -> Dict[str, int]:
    """Each proof's sunk cost, keyed by proof id.

    A property of the proof, not of the box, so every opinion from one proof reports the
    same figure and the largest is taken rather than the sum -- adding them up would
    multiply one sacrifice by the number of boxes it is spread over.
    """
    backing: Dict[str, int] = {}
    for opinion in opinions:
        backing[opinion.proof_id] = max(
            backing.get(opinion.proof_id, 0), opinion.burned_nanoerg
        )
    return backing


def totals(opinions: Iterable[Opinion]) -> ReputationTotals:
    """The for-and-against summary of ``opinions``.

    A proof whose boxes cancel out contributes nothing and is counted in neither
    column: it published a stake for and an equal stake against, and calling that
    either a supporter or a detractor would be a claim it did not make. Its backing
    cancels with it, for the same reason.
    """
    opinions = list(opinions)
    verdicts = by_proof(opinions)
    burned = burned_by_proof(opinions)
    return ReputationTotals(
        positive=sum(value for value in verdicts.values() if value > 0),
        negative=-sum(value for value in verdicts.values() if value < 0),
        positive_proofs=sum(1 for value in verdicts.values() if value > 0),
        negative_proofs=sum(1 for value in verdicts.values() if value < 0),
        positive_backing=sum(
            value * burned.get(proof, 0) for proof, value in verdicts.items() if value > 0
        ),
        negative_backing=-sum(
            value * burned.get(proof, 0) for proof, value in verdicts.items() if value < 0
        ),
    )


@dataclass(frozen=True)
class NodeReputation:
    """Everything the ledgers say about one node, with its own voice set aside.

    A node publishes an opinion about itself -- that is the box carrying its address,
    and the one its proof id is discovered from -- so the chain's answer to "what is
    said about this node" always includes the node itself. It is kept here rather than
    dropped, and kept *apart* rather than counted: a node vouching for itself is not
    reputation, and an operator who cannot see it would wonder where the stake behind
    their own proof went.
    """

    node_id: str
    #: This node's own reputation proof, whose opinions are in :attr:`own`.
    own_proof_id: str
    #: What everybody else stakes on this node.
    opinions: List[Opinion]
    #: What this node's own proof stakes on it.
    own: List[Opinion]
    #: Ledger tag -> why that ledger could not be read, for the ones that failed.
    errors: Dict[str, str]

    @property
    def totals(self) -> ReputationTotals:
        return totals(self.opinions)


def split_own(opinions: Iterable[Opinion], own_proof_id: str) -> Tuple[List[Opinion], List[Opinion]]:
    """``(others, own)``, splitting on which proof published each opinion.

    Identity is the proof id, not the owner wallet: the wallet behind a proof is on the
    box (R7), but two proofs owned by one wallet are still two publishers, and only the
    proof this node publishes through is this node's own voice.
    """
    opinions = list(opinions)
    if not own_proof_id:
        return opinions, []
    own = [opinion for opinion in opinions if opinion.proof_id == own_proof_id]
    others = [opinion for opinion in opinions if opinion.proof_id != own_proof_id]
    return others, own


