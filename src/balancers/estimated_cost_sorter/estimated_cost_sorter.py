from math import log
from typing import Dict, Tuple, Generator
from statistics import mean
from protos import celaut_pb2
from src.reputation_system.interface import compute_reputation
from src.utils.cost_functions.general_cost_functions import normalized_maintain_cost as nmc
from src.utils.cost_functions.variance_cost_normalization import variance_cost_normalization as vcnorm
from src.utils.env import EnvManager
from src.utils.utils import from_gas_amount
from src.utils.logger import LOGGER as logger
from src.database.sql_connection import SQLConnection
from src.payment_system.contracts.ergo.interface import LEDGER as ERGO_LEDGER, CONTRACT_HASH as ERGO_CONTRACT_HASH

env_manager = EnvManager()
SOCIALIZATION_FACTOR = float(env_manager.get_env("SOCIALIZATION_FACTOR"))
INIT_COST_CONFIGURATION_FACTOR = env_manager.get_env("INIT_COST_CONFIGURATION_FACTOR")
MAINTENANCE_COST_CONFIGURATION_FACTOR = env_manager.get_env("MAINTENANCE_COST_CONFIGURATION_FACTOR")
GAS_PER_ERG = int(env_manager.get_env("GAS_PER_ERG"))

sq = SQLConnection()

def estimated_cost_sorter(estimated_costs: Dict[str, celaut_pb2.EstimatedCost]) -> Generator[Tuple[str, celaut_pb2.EstimatedCost], None, None]:
    
    total_reputation: float = sq.total_peer_reputation()
    
    def __compute_score(peer_id: str, estimated_cost: celaut_pb2.EstimatedCost) -> float:

        gas_cost: int = int(
            INIT_COST_CONFIGURATION_FACTOR * vcnorm(
                cost=from_gas_amount(estimated_cost.cost),
                variance=estimated_cost.variance
            )
            +
            MAINTENANCE_COST_CONFIGURATION_FACTOR * int(
                mean([
                    vcnorm(
                        cost=nmc(
                            cost=from_gas_amount(estimated_cost.init_maintenance_cost),
                            timelapse=estimated_cost.maintenance_seconds_loop
                        ),
                        variance=estimated_cost.variance
                    ),
                    vcnorm(
                        cost=nmc(
                            cost=from_gas_amount(estimated_cost.max_maintenance_cost),
                            timelapse=estimated_cost.maintenance_seconds_loop
                        ),
                        variance=estimated_cost.variance
                    )
                ])
            )
        )

        local_gas_per_erg: int = GAS_PER_ERG
        
        if local_gas_per_erg:

            if peer_id != "local":
                from typing import Optional
                peer_gas_per_erg: Optional[int] = sq.get_peer_gas_price(peer_id=peer_id, contract_hash=ERGO_CONTRACT_HASH, ledger_id=ERGO_LEDGER)
                if peer_gas_per_erg is None:
                    logger(f"No ergo gas price on peer {peer_id}, continue.")
                    return 0
            else:
                peer_gas_per_erg = local_gas_per_erg

            erg_per_gas_unit = 1 / (peer_gas_per_erg / local_gas_per_erg)
            normalized_cost_in_ergs = gas_cost * erg_per_gas_unit
        
        else:
            normalized_cost_in_ergs = 0.0
        
        if peer_id == 'local':
            reputation: float = 1 
        else:
            reputation: float = ((compute_reputation(peer_id=peer_id) / total_reputation) if total_reputation else 0 ) * SOCIALIZATION_FACTOR  # TODO Should be improved, because should not be relation on how many peers are.
        
        score = reputation - log(normalized_cost_in_ergs)  # TODO Could have weights on envs.
        
        logger(f"Computing estimated cost score for peer {peer_id}: reputation {reputation}, cost {log(normalized_cost_in_ergs)} => score {score}\n")
        return score

    return (
        (_id, estimated_cost) for _id, estimated_cost in
        sorted(
            estimated_costs.items(),
            key=lambda item: __compute_score(item[0], item[1]),
            reverse=True
        )
    )
