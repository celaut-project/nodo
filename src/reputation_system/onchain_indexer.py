"""Read what the ledgers say about each known peer into SQLite. Runs on the periodic tick.

The same three properties the donation indexer keeps, for the same reasons
(:mod:`src.payment_system.donations.indexer`):

* **Local, never announced.** Every node reads the chain itself. A standing a peer sent
  us about itself would be self-declared, and therefore forgeable.
* **Never on the routing path.** The balancer reads aggregates out of SQLite and does no
  network I/O at all (:mod:`.onchain_credit`). This is what fills those rows in. An
  explorer read inside ``estimated_cost_sorter`` would put an unreachable host in the
  middle of every launch.
* **Never raises into a caller.** An unreachable explorer is *undetermined*, not a
  verdict of "nobody vouches for this peer" -- the rows already stored stand, and the
  next refresh re-reads what this one could not.

Two filters are applied before a row is written, and they are what the whole design rests
on:

**The subject's own proofs are set aside** (:func:`interface._own_proof_ids`, issue
#351). Hygiene rather than the defence: the list is what the subject *chose to announce*,
and minting a second proof it never mentions is free.

**A publisher has to be a peer we know, and to have proved it owns the proof.** The proof
id has to appear in some peer's stored advertisement *and* carry an owner attestation
that verifies against that peer's id (:func:`proof_attestation.attested_proof_owner`) --
the same link ``nodo verify_reputation`` prints and ``manager.add_peer_instance`` checks
when a peer introduces itself. Both checks are local: one SQLite read and one Schnorr
verification, no round trip. The canonical-contract question is already settled upstream,
because ``opinions_about`` only returns boxes on the pinned ErgoTree.

An opinion published by a proof we cannot attribute to a peer is **dropped rather than
stored as unattributed**, which is where this parts from the donation indexer. A donation
is a fact about money that happened and can be credited retroactively when its donor is
introduced; an opinion is re-read in full on every refresh, so a publisher introduced
later is picked up on the next pass without a row having waited for it.
"""
from __future__ import annotations

from time import monotonic
from typing import Dict, List, Optional, Tuple

from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER


def publisher_peers() -> Dict[str, str]:
    """``proof id -> peer id`` for every proof a known peer has proved it owns.

    Read from the advertisement each peer signed, which is where
    ``commands.verify_reputation`` reads them from too: a proof announced there is one
    the peer can be asked to prove it owns, so it is the list the peer is accountable
    for. A node may announce several.

    A proof announced by two peers is attributed to neither. Only one of them can control
    it, and crediting the wrong one would hand a peer the voice another peer paid for --
    the same rule ``peer_by_contract_instance`` applies to a payment address.
    """
    from protos import celaut_pb2
    from src.reputation_system.proof_attestation import attested_proof_owner
    from src.utils.contract_xattrs import get_token_id

    sql = SQLConnection()
    owners: Dict[str, str] = {}
    contested: set = set()
    for peer_id in sql.get_peers_id():
        advertisement = None
        try:
            advertisement = sql.get_peer_advertisement(peer_id)
        except Exception as e:
            LOGGER(f"Could not read the advertisement of peer {peer_id}: {e}")
        if not advertisement:
            continue
        announced = celaut_pb2.Peer()
        try:
            announced.ParseFromString(advertisement)
        except Exception as e:
            LOGGER(f"Unreadable advertisement for peer {peer_id}: {e}")
            continue
        for contract in announced.reputation_proofs:
            token_id = get_token_id(contract)
            if not token_id:
                continue
            # The attestation is what ties an on-chain proof to a node identity: R7
            # holds an Ergo proposition and can never hold an identity key, so the
            # wallet signs the peer id instead. A proof announcing an owner it cannot
            # prove is worth exactly as much as one announcing none.
            if not attested_proof_owner(contract, peer_id):
                continue
            if token_id in owners and owners[token_id] != peer_id:
                contested.add(token_id)
                continue
            owners[token_id] = peer_id

    for token_id in contested:
        LOGGER(
            f"Reputation proof {token_id} is announced by more than one peer; its "
            "opinions are attributed to none of them."
        )
        owners.pop(token_id, None)
    return owners


def opinions_for(subject_id: str, owners: Dict[str, str]) -> List[Tuple[str, str, float]]:
    """``(proof id, publisher peer id, verdict)`` for one subject. Raises on a read failure.

    The verdict is the publisher's netted signed share of itself staked on the subject
    (``opinions.by_proof``), so a publisher that split its stake into ten boxes still
    speaks once. Raising rather than returning nothing is deliberate and matches
    ``opinions_about``: an empty list means "the ledgers hold no opinion about this
    peer", and a failed read must not be able to say that.
    """
    from src.reputation_system.interface import _own_proof_ids, _opinion_readers
    from src.reputation_system.opinions import by_proof, split_own

    collected = []
    for ledger, reader in _opinion_readers().items():
        collected.extend(reader(subject_id))

    opinions, _own = split_own(collected, _own_proof_ids(subject_id))
    return [
        (proof_id, owners[proof_id], verdict)
        for proof_id, verdict in by_proof(opinions).items()
        if proof_id in owners
    ]


def refresh() -> int:
    """Re-read every known peer's on-chain standing. Returns how many rows were written.

    Our own proof is not in ``owners`` unless we happen to be our own peer row, and that
    is the intended shape: what we publish is our *local* scores (``submit_to_ledger``),
    which the balancer already weighs at ``SOCIALIZATION_FACTOR``. Reading them back in
    would count one observation twice, once unpurchasably and once through a channel that
    can be bought.
    """
    from src.reputation_system.envs import LEDGER

    sql = SQLConnection()
    owners = publisher_peers()
    if not owners:
        # Nobody we know has proved it publishes anything, so no opinion out there is
        # attributable and the term is zero for every candidate. Not an error: it is
        # what a young node looks like.
        return 0

    written = 0
    for peer_id in sql.get_peers_id():
        try:
            rows = opinions_for(peer_id, owners)
        except Exception as e:
            # Includes the explorer being unreachable. The rows already stored stand:
            # replacing them with nothing would read as "the network withdrew its
            # opinion", which is a verdict this failure cannot support.
            LOGGER(f"Could not read the on-chain reputation of peer {peer_id}: {e}")
            continue
        if sql.replace_onchain_opinions(LEDGER, peer_id, rows):
            written += len(rows)
    return written


# How often the on-chain standings are re-read. A constant rather than a setting, for the
# reasons the donation index gives: opinions move on the timescale of a transaction at
# best, the term they feed only ever breaks a tie between candidates, and every extra
# knob is one an operator would have to reason about to gain nothing. An hour also keeps
# the explorer read well clear of the routing path.
REFRESH_INTERVAL_SECONDS = 3600
_last_refresh: Optional[float] = None


def tick() -> None:
    """Refresh the standings if they are due. Self-gating, and never raises.

    Called from the manager's short-interval loop, the same way the donation, energy and
    DDNS ticks are: that loop runs every few seconds, so the gate is here rather than in
    the caller's schedule.
    """
    global _last_refresh
    now = monotonic()
    if _last_refresh is not None and now - _last_refresh < REFRESH_INTERVAL_SECONDS:
        return
    _last_refresh = now
    try:
        written = refresh()
    except Exception as e:
        LOGGER(f"On-chain reputation refresh failed: {e}")
        return
    if written:
        LOGGER(f"Read {written} attributable on-chain opinion(s).")
    # Unconditionally, not only when something was written: a refresh that removed every
    # row changed the answer just as much as one that added some, and the standings are
    # cached off the routing path.
    from src.reputation_system.onchain_credit import forget_cached_standings

    forget_cached_standings()
