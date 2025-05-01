from math import log
from typing import Dict, Tuple, Generator
from statistics import mean
from protos import gateway_pb2
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
ERGO_GAS_COST = env_manager.get_env("ERGO_GAS_COST")

sq = SQLConnection()

def estimated_cost_sorter(
        estimated_costs: Dict[str, gateway_pb2.EstimatedCost],
        weight_clauses: Dict[int, int]
) -> Generator[Tuple[str, gateway_pb2.EstimatedCost], None, None]:
    
    total_reputation: float = sq.total_peer_reputation()
    
    def __compute_score(peer_id: str, estimated_cost: gateway_pb2.EstimatedCost) -> float:
        priority: int = max(1, weight_clauses[estimated_cost.comb_resource_selected])  # If the combinational resource clause don't have a cost_weight, it's like equal to 1 cost weight.

        gas_cost: int = sum([
            
            # Normaliced initialization cost.
            INIT_COST_CONFIGURATION_FACTOR * vcnorm(
                cost=from_gas_amount(estimated_cost.cost),
                variance=estimated_cost.variance
            ),
            
            # Normalized maintenance cost.
            MAINTENANCE_COST_CONFIGURATION_FACTOR * int(
                mean([
                    
                    # Minimum maintenance cost
                    vcnorm(
                        cost=nmc(
                            cost=from_gas_amount(estimated_cost.min_maintenance_cost),
                            timelapse=estimated_cost.maintenance_seconds_loop
                        ),
                        variance=estimated_cost.variance
                    ),
                    
                    # Maximum maintenance cost
                    vcnorm(
                        cost=nmc(
                            cost=from_gas_amount(estimated_cost.max_maintenance_cost),
                            timelapse=estimated_cost.maintenance_seconds_loop
                        ),
                        variance=estimated_cost.variance
                    )
                ])
            )
        ])

        local_erg_gas: int = ERGO_GAS_COST
        
        if peer_id != "local":
            peer_erg_gas: int = sq.get_peer_gas_price(peer_id=peer_id, contract_hash=ERGO_CONTRACT_HASH, ledger_id=ERGO_LEDGER)
            if peer_erg_gas is None:
                logger(f"No ergo gas price on peer {peer_id}, continue.")
                return 0
        else:
            peer_erg_gas = ERGO_GAS_COST

        norm_gas_cost: int = gas_cost * (peer_erg_gas / local_erg_gas) if local_erg_gas else 0
        
        if peer_id == 'local':
            reputation: float = 1 
        else:
            reputation: float = ((compute_reputation(peer_id=peer_id) / total_reputation) if total_reputation else 0 ) * SOCIALIZATION_FACTOR  # TODO Should be improved, because should not be relation on how many peers are.
        
        score = priority + reputation - log(norm_gas_cost)  # TODO Could have weights on envs.
        
        logger(f"Computing estimated cost score for peer {peer_id}: priority {priority}, reputation {reputation}, cost {log(norm_gas_cost)} => score {score}\n")
        return score

    return (
        (_id, estimated_cost) for _id, estimated_cost in
        sorted(
            estimated_costs.items(),
            key=lambda item: __compute_score(item[0], item[1]),
            reverse=True
        )
    )
