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

What is stored is the chain as it stands: every proof's netted verdict on each known
peer, and the sunk cost behind that proof. **No publisher is filtered out here**, and
that is the design rather than an omission. A proof is not credible because of who owns
it -- proofs and wallets are both free to mint, so any identity test is a test an
attacker passes by paying nothing. It is credible because of *what it has said*, which
:mod:`.onchain_credit` scores at read time against this node's own opinions. A proof
nobody here has ever heard of is not excluded from the table; it simply agrees with us
about nothing, and nothing is what its burn is then worth.

One filter does apply: **the subject's own proofs are set aside**
(:func:`interface._own_proof_ids`, issue #351). Hygiene rather than the defence, and it
is worth being clear about which is which. The announced list is what the subject
*chose* to disclose, and minting a second proof it never mentions is free -- so this
keeps a self-vouch out of the figures a node reports about itself, and stops nothing.
What stops the self-vouch is that an undisclosed proof has no track record with us
either.

The rows are keyed by ``(ledger, subject, proof)``: the publishing proof is the unit of
voice, netted across its boxes before it gets here (``opinions.by_proof``), so a proof
that split its stake into ten boxes still speaks once.
"""
from __future__ import annotations

from time import monotonic
from typing import Dict, List, Optional, Tuple

from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER


def opinions_for(subject_id: str) -> List[Tuple[str, float, int]]:
    """``(proof id, verdict, burned nanoERG)`` for one subject. Raises on a read failure.

    The verdict is the publishing proof's netted signed share of itself staked on the
    subject (``opinions.by_proof``), in ``[-1, 1]``. The burn is a property of the proof
    rather than of a box (``opinions.burned_by_proof``), so it is the same figure on
    every row that proof produces and it is stored per row only so that the reader needs
    one table and no join.

    Raising rather than returning nothing is deliberate and matches ``opinions_about``:
    an empty list means "the ledgers hold no opinion about this peer", and a failed read
    must not be able to say that.
    """
    from src.reputation_system.interface import _own_proof_ids, _opinion_readers
    from src.reputation_system.opinions import burned_by_proof, by_proof, split_own

    collected = []
    for ledger, reader in _opinion_readers().items():
        collected.extend(reader(subject_id))

    opinions, _own = split_own(collected, _own_proof_ids(subject_id))
    burned = burned_by_proof(opinions)
    return [
        (proof_id, verdict, burned.get(proof_id, 0))
        for proof_id, verdict in by_proof(opinions).items()
    ]


def refresh() -> int:
    """Re-read every known peer's on-chain standing. Returns how many rows were written.

    One pass over the peers we know, and the table it leaves behind serves both of the
    reader's needs at once: the rows for one subject are the verdicts on that candidate,
    and the rows grouped by proof are each publisher's opinion vector over the peers we
    can check it against. The agreement score costs no extra request because the scan
    that feeds it is the scan that was already happening.

    Our own proof is read like any other. What it says about a peer is our own local
    score republished (``submit_to_ledger``), so it agrees with us by construction and
    would lend itself perfect credibility -- which is why :mod:`.onchain_credit` drops
    it at read time rather than here: the row is still the truth about the chain, and it
    is the scoring, not the index, that must not count one observation twice.
    """
    from src.reputation_system.envs import LEDGER

    sql = SQLConnection()
    written = 0
    for peer_id in sql.get_peers_id():
        try:
            rows = opinions_for(peer_id)
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
        LOGGER(f"Read {written} on-chain opinion(s).")
    # Unconditionally, not only when something was written: a refresh that removed every
    # row changed the answer just as much as one that added some, and the standings are
    # cached off the routing path.
    from src.reputation_system.onchain_credit import forget_cached_standings

    forget_cached_standings()
