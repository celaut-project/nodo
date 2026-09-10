"""``nodo reputation`` -- what the network stakes on this node, and when it did.

The counterpart to ``verify_reputation``, which asks whether a *peer's* proof is real.
This asks the question about us: of every reputation proof out there, which ones have
put part of themselves behind this node, how much, for or against, and how recently.

Two audiences, one computation. An operator reads the printed form; the TUI's EARNINGS
page reads ``--json``, which is why this is a command and not a page-local query -- the
lookup needs an explorer, and the TUI must not block a frame on the network.

Read-only: explorer reads and one config lookup. Nothing is signed, nothing is spent.
"""

import json
import sys
import time
from typing import List, Optional

from src.reputation_system.opinions import (
    NodeReputation,
    Opinion,
    ReputationTotals,
    totals,
)

DAY_SECONDS = 24 * 60 * 60
# ERG <-> nanoERG is fixed by the Ergo protocol, not configurable, so it lives here as a
# constant. A proof's backing is on-chain ERG and is never converted through MU: MU is
# this node's unit of account for what it charges, and somebody else's sunk cost is not
# a balance of ours (the same line the TUI's wallet card draws).
NANOERG_PER_ERG = 10 ** 9


def _totals_json(figures: ReputationTotals) -> dict:
    return {
        "positive": figures.positive,
        "negative": figures.negative,
        "net": figures.net,
        "positive_proofs": figures.positive_proofs,
        "negative_proofs": figures.negative_proofs,
        "positive_backing": figures.positive_backing,
        "negative_backing": figures.negative_backing,
        "net_backing": figures.net_backing,
    }


def _opinion_json(opinion: Opinion) -> dict:
    return {
        "ledger": opinion.ledger,
        "proof_id": opinion.proof_id,
        "owner": opinion.owner,
        "amount": opinion.amount,
        "assigned_amount": opinion.assigned_amount,
        "weight": opinion.weight,
        "positive": opinion.positive,
        "published_at": opinion.published_at,
        "box_id": opinion.box_id,
        "burned_nanoerg": opinion.burned_nanoerg,
        "backed_nanoerg": opinion.backed_nanoerg,
    }


def report(reputation: NodeReputation, now: Optional[int] = None) -> dict:
    """``reputation`` as the JSON the TUI reads and this command prints from.

    A standing and what backs it, with no per-window breakdown. Reputation is a stock
    and not a flow, and the chain cannot be made to answer "earned this week" from
    unspent boxes: revising an opinion spends its box and writes a new one, and nodo
    re-splits its whole supply on every submission, so every date resets at once. A
    window over those dates would report the publisher's submission cadence. Each
    opinion carries its own ``published_at`` for what that is worth -- the age of the
    box -- and nothing is aggregated from it.
    """
    now = int(time.time()) if now is None else now
    return {
        "node_id": reputation.node_id,
        "own_proof_ids": list(reputation.own_proof_ids),
        "read_at": now,
        "errors": reputation.errors,
        "standing": _totals_json(totals(reputation.opinions)),
        # Ordered by what stands behind them rather than by share, once, so every
        # reader of this report shows them in the same order: a proof that sacrificed
        # real ERG is the one worth reading first, and a free proof committing all of
        # itself would otherwise head the list.
        "opinions": [
            _opinion_json(opinion)
            for opinion in sorted(
                reputation.opinions,
                key=lambda item: (item.backed_nanoerg, item.weight),
                reverse=True,
            )
        ],
        "own": [_opinion_json(opinion) for opinion in reputation.own],
    }


def _format_share(value: float) -> str:
    """A share of a proof, as a percentage of it.

    Percent rather than the raw fraction because that is what the quantity is: the
    portion of what its publisher has assigned to opinions that is committed here.
    Three decimals, since a proof splitting its assigned supply a thousand ways still
    says something.
    """
    return f"{value * 100:.3f}%"


def _format_erg(nanoerg: float) -> str:
    """A sunk-cost figure in ERG. Never in MU -- see :data:`NANOERG_PER_ERG`."""
    return f"{nanoerg / NANOERG_PER_ERG:.6f} ERG"


def _format_when(published_at: Optional[int], now: int) -> str:
    if published_at is None:
        return "undated"
    days = max(0, (now - published_at) // DAY_SECONDS)
    if days == 0:
        return "today"
    return f"{days}d ago"


def _print_report(data: dict) -> None:
    # Named by the node it is about, because the same command answers for a peer.
    print(f"Reputation held on node {data['node_id']}")
    print("=" * 50)
    # The subject's own proofs, not ours: the same command answers for a peer, and
    # printing our proof id under a peer's name said the opposite of what it meant.
    own_proofs = data.get("own_proof_ids") or []
    print(
        "Publishes through: "
        + (", ".join(own_proofs) if own_proofs else "no proof announced")
    )

    partial = bool(data["errors"])
    for ledger, error in (data["errors"] or {}).items():
        print(f"WARNING: {ledger} could not be read: {error}")
    if partial:
        # Everything below covers only the ledgers that answered, so it is a floor and
        # not the figure. Saying so is the difference between a partial answer and a
        # wrong one.
        print("The figures below cover only the ledgers that answered.")

    standing = data["standing"]
    print()
    print(
        f"Standing: +{_format_share(standing['positive'])} "
        f"/ -{_format_share(standing['negative'])} "
        f"(net {_format_share(standing['net'])}) "
        f"from {standing['positive_proofs']} for and "
        f"{standing['negative_proofs']} against"
    )
    # What those shares cost the people who published them. Minting a proof is free;
    # the ERG behind one cannot be taken back out, so this is the figure that says
    # whether a share was expensive to fabricate.
    print(
        f"Backed by:  {_format_erg(standing['positive_backing'])} sunk for "
        f"/ {_format_erg(standing['negative_backing'])} against"
    )

    print()
    if not data["opinions"]:
        print(
            "No opinion could be read from the ledgers that answered."
            if partial
            else "No proof out there has staked anything on this node yet."
        )
    else:
        print("Who staked what")
        print("-" * 50)
        for opinion in data["opinions"]:
            sign = "+" if opinion["positive"] else "-"
            print(
                f"  {sign}{_format_share(opinion['weight'])}  "
                f"backed by {_format_erg(opinion.get('backed_nanoerg', 0))}  "
                f"proof {opinion['proof_id']}  "
                f"({opinion['amount']} of the {opinion['assigned_amount']} tokens that "
                f"proof has assigned, "
                f"{_format_when(opinion['published_at'], data['read_at'])})"
            )

    if data["own"]:
        print()
        print(
            "Its own proof stakes "
            + ", ".join(
                f"{'+' if opinion['positive'] else '-'}{_format_share(opinion['weight'])}"
                for opinion in data["own"]
            )
            + " on itself, and is left out of the figures above: a node vouching for "
            "itself is not reputation. "
            + _format_erg(sum(o.get("burned_nanoerg", 0) for o in data["own"][:1]))
            + " is sunk into that proof."
        )


def reputation(argv: Optional[List[str]] = None) -> bool:
    """Print this node's on-chain reputation. ``True`` when the chain answered.

    ``nodo reputation [<node id>] [--json]``. The optional id is a node's identity
    public key -- a peer id, so a peer can be asked about with the same command and the
    same shell completion.
    """
    from src.reputation_system.interface import get_node_reputation

    argv = list(argv or [])
    as_json = "--json" in argv
    positional = [argument for argument in argv if not argument.startswith("--")]
    node_id = positional[0] if positional else None

    try:
        data = report(get_node_reputation(node_id=node_id))
    except Exception as e:
        if as_json:
            # Shaped like a report so a reader never has to branch on which kind of
            # object it got; an error instead of totals, never zeros passed off as
            # an answer.
            json.dump({"error": str(e), "read_at": int(time.time())}, sys.stdout)
            print()
        else:
            print(f"Could not read this node's reputation: {e}")
        return _flushed(False)

    if as_json:
        json.dump(data, sys.stdout)
        print()
    else:
        _print_report(data)
    return _flushed(not data["errors"])


def _flushed(result: bool) -> bool:
    """Return ``result``, having put everything printed on the wire first.

    ``nodo.py`` ends a command with ``os._exit``, which does not run interpreter
    shutdown, so a buffered stdout -- which is what stdout is whenever this is piped,
    as the TUI pipes it -- would be discarded and the caller would read nothing at all.
    """
    sys.stdout.flush()
    return result
