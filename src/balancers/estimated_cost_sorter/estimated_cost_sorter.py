from hashlib import sha3_256
from math import log
from typing import Dict, Tuple, Generator
from statistics import mean
from protos import celaut_pb2
from src.reputation_system.interface import compute_reputation
from src.utils.cost_functions.variance_cost_normalization import variance_cost_normalization as vcnorm
from src.utils.config import ConfigManager
from src.utils.utils import from_amount
from src.utils.logger import LOGGER as logger
from src.utils.monetary import HOUR_SECONDS, format_mu
from src.database.sql_connection import SQLConnection

env_manager = ConfigManager()
SOCIALIZATION_FACTOR = float(env_manager.get("SOCIALIZATION_FACTOR"))
ERGO_LEDGER = "ergo"
ERGO_CONTRACT_HASH = sha3_256("proveDlog(decodePoint())".encode("utf-8")).hexdigest()

sq = SQLConnection()

def estimated_cost_sorter(estimated_costs: Dict[str, celaut_pb2.EstimatedCost]) -> Generator[Tuple[str, celaut_pb2.EstimatedCost], None, None]:
    
    total_reputation: float = sq.total_peer_reputation()
    
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

        if peer_id == 'local':
            reputation: float = 1
        else:
            reputation: float = ((compute_reputation(peer_id=peer_id) / total_reputation) if total_reputation else 0 ) * SOCIALIZATION_FACTOR  # TODO Should be improved, because should not be relation on how many peers are.

        # A free peer is the best possible offer, not a math domain error. log(0) used
        # to raise here, and a price of zero is reachable now that a node can give its
        # capacity away (free_tier).
        score = reputation - log(cost_mu) if cost_mu > 0 else float('inf')

        logger(
            f"Estimated cost score for peer {peer_id}: reputation {reputation}, "
            f"cost {format_mu(cost_mu)}/h => score {score}\n"
        )
        return score

    return (
        (_id, estimated_cost) for _id, estimated_cost in
        sorted(
            estimated_costs.items(),
            key=lambda item: __compute_score(item[0], item[1]),
            reverse=True
        )
    )
