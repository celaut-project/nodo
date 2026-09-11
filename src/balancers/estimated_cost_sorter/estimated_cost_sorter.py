from typing import Dict, Tuple, Generator
from statistics import mean
from protos import celaut_pb2
from src.balancers.scoring import DEFAULT_REPUTATION_HALF_CREDIT, reputation_factor, score
from src.payment_system.donations.credit import bonus_by_peer
from src.reputation_system.interface import compute_reputation
from src.reputation_system.onchain_credit import (
    DEFAULT_ONCHAIN_HALF_CREDIT,
    DEFAULT_ONCHAIN_WEIGHT,
    standing_by_peer,
)
from src.utils.cost_functions.variance_cost_normalization import variance_cost_normalization as vcnorm
from src.utils.config import ConfigManager
from src.utils.utils import from_amount
from src.utils.logger import LOGGER as logger
from src.utils.monetary import HOUR_SECONDS, format_mu
from src.database.sql_connection import LOCAL_PEER_ID

env_manager = ConfigManager()

# This module used to recompute Ergo's ledger tag and contract hash for itself, which
# is how a balancer ends up knowing which chain a node settles on. It does not need to:
# ranking a candidate is about price, reliability and donations, and none of the three
# is a property of a ledger. The registry is where contracts are named.


def _parameter(key: str, default: float) -> float:
    """One parameter of the selection formula, from ``balancers:``.

    Read per call rather than captured at import, so a config the operator edited
    through the TUI takes effect on the next routing decision instead of the next
    restart. Read by its explicit path, too: ``ConfigManager.get`` would still find a
    dotless key by scanning every section, but relying on that leaves the key with no
    unambiguous home -- and these have one now.
    """
    raw = env_manager.get(f"balancers.{key}", default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger(f"balancers.{key}={raw!r} is not a number; using {default}.")
        return float(default)


def estimated_cost_sorter(estimated_costs: Dict[str, celaut_pb2.EstimatedCost]) -> Generator[Tuple[str, celaut_pb2.EstimatedCost], None, None]:

    reputation_weight: float = _parameter("SOCIALIZATION_FACTOR", 2.0)
    reputation_half: float = _parameter("REPUTATION_HALF_CREDIT", DEFAULT_REPUTATION_HALF_CREDIT)
    donation_weight: float = _parameter("DONATION_WEIGHT", 0.3)
    local_bias: float = _parameter("LOCAL_BIAS", 1.0)
    onchain_weight: float = _parameter("ONCHAIN_REPUTATION_WEIGHT", DEFAULT_ONCHAIN_WEIGHT)

    # One read of the donation index for the whole sort, out of SQLite. Zero network
    # I/O in a routing decision: an unreachable explorer, or an index that has never
    # been filled, leaves this empty -- and then every candidate scores zero on the
    # donation term, never some of them.
    donation_bonuses: Dict[str, float] = bonus_by_peer()

    # And one read of the on-chain standings, on the same terms and for the same reason
    # (issue #353). This is what the chain says about each candidate, already weighed by
    # how much this node trusts each publisher and capped per publisher -- see
    # `reputation_system.onchain_credit`. It is scored at its **own** weight, never at
    # SOCIALIZATION_FACTOR: the local term is an observation nobody can buy, and this one
    # is published by proofs whose voice is for sale. The ratio
    # ONCHAIN_REPUTATION_WEIGHT / DONATION_WEIGHT is the exchange rate between destroying
    # an ERG and donating one, and it favours donating by construction.
    onchain_standings: Dict[str, float] = standing_by_peer()

    def __compute_score(peer_id: str, estimated_cost: celaut_pb2.EstimatedCost) -> float:

        if not hasattr(estimated_cost, 'cost') or \
            not hasattr(estimated_cost.cost, 'n') or \
            not hasattr(estimated_cost, 'init_maintenance_cost') or \
            not hasattr(estimated_cost.init_maintenance_cost, 'n') or \
            not hasattr(estimated_cost, 'max_maintenance_cost') or \
            not hasattr(estimated_cost.max_maintenance_cost, 'n') or \
            not hasattr(estimated_cost, 'maintenance_seconds_loop') or \
            not hasattr(estimated_cost, 'variance'):
            logger(f"Estimated cost for peer {peer_id} is missing required fields, skipping. Estimated cost: {estimated_cost}")
            return float('inf')  # Assign a very high cost to skip this peer

        # Every node quotes in MU, and MU is pegged, so two peers' estimates are
        # directly comparable. This used to convert through each node's own
        # gas-per-ERG factor (`1 / (peer_gas_per_erg / local_gas_per_erg)`), which only
        # existed because "gas" meant something different on every node.
        def maintenance_mu_per_hour(amount) -> int:
            seconds = estimated_cost.maintenance_seconds_loop
            if seconds <= 0:
                return 0
            return int(
                vcnorm(cost=from_amount(amount), variance=estimated_cost.variance)
                * HOUR_SECONDS
                / seconds
            )

        cost_mu: int = int(
            vcnorm(cost=from_amount(estimated_cost.cost), variance=estimated_cost.variance)
            + mean([
                maintenance_mu_per_hour(estimated_cost.init_maintenance_cost),
                maintenance_mu_per_hour(estimated_cost.max_maintenance_cost),
            ])
        )

        is_local = peer_id == 'local'
        # Our own donations are read off the chain and counted with the same list as
        # anybody else's -- our wallet is our identity, so it is the same code path with
        # no special case. If this node funds someone it does not itself count, it earns
        # nothing here and slightly disfavours itself, which is honest: an operator who
        # does not recognise a contribution should not bill themselves credit for it.
        #
        # Translated, because two naming conventions meet here: this candidate is
        # 'local' to the execution balancer and LOCAL_PEER_ID in `contract_instance`,
        # which is what the donation index is keyed by. Looking it up by the balancer's
        # name reads as zero -- and reads exactly like a node that has never donated.
        donation_bonus: float = donation_bonuses.get(
            LOCAL_PEER_ID if is_local else peer_id, 0.0
        )
        reputation: float = 0.0 if is_local else compute_reputation(peer_id=peer_id)
        # Like the reputation term, and for the same reason: this node holds no evidence
        # about itself, and what the chain says about us is what we published. Counting
        # it here would let a node raise its own rank against its peers by burning.
        onchain: float = 0.0 if is_local else onchain_standings.get(peer_id, 0.0)

        candidate_score = score(
            cost_mu=cost_mu,
            reputation=reputation,
            reputation_weight=reputation_weight,
            reputation_half_credit=reputation_half,
            donation_bonus=donation_bonus,
            donation_weight=donation_weight,
            onchain_reputation=onchain,
            onchain_weight=onchain_weight,
            local_bias=local_bias if is_local else None,
        )

        # Every term, broken out. A non-zero donation default has to be auditable: an
        # operator must be able to read why one candidate beat another, and a single
        # score cannot say whether it was the price, the reliability or the donation.
        if is_local:
            standing = f"local bias {local_bias:+.4f}"
        else:
            standing = (
                f"reputation {reputation:+.2f} -> {reputation_weight * reputation_factor(reputation, reputation_half):+.4f}"
                f", on-chain {onchain:+.4f} -> {onchain_weight * onchain:+.4f}"
            )
        logger(
            f"Estimated cost score for peer {peer_id}: cost {format_mu(cost_mu)}/h, "
            f"{standing}, donation {donation_bonus:.4f} -> "
            f"{donation_weight * donation_bonus:+.4f} => score {candidate_score}\n"
        )
        return candidate_score

    return (
        (_id, estimated_cost) for _id, estimated_cost in
        sorted(
            estimated_costs.items(),
            key=lambda item: __compute_score(item[0], item[1]),
            reverse=True
        )
    )
