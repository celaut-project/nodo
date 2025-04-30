from typing import Dict, Tuple, Generator
from statistics import mean
from protos import gateway_pb2
from src.reputation_system.interface import compute_reputation
from src.utils.cost_functions.general_cost_functions import normalized_maintain_cost as nmc
from src.utils.cost_functions.variance_cost_normalization import variance_cost_normalization as vcnorm
from src.utils.env import EnvManager
from src.utils.utils import from_gas_amount
from src.utils.logger import LOGGER as log

env_manager = EnvManager()
SOCIALIZATION_FACTOR = env_manager.get_env("SOCIALIZATION_FACTOR")
WEIGHT_CONFIGURATION_FACTOR = env_manager.get_env("WEIGHT_CONFIGURATION_FACTOR")
INIT_COST_CONFIGURATION_FACTOR = env_manager.get_env("INIT_COST_CONFIGURATION_FACTOR")
MAINTENANCE_COST_CONFIGURATION_FACTOR = env_manager.get_env("MAINTENANCE_COST_CONFIGURATION_FACTOR")
ERGO_GAS_COST = env_manager.get_env("ERGO_GAS_COST")

def estimated_cost_sorter(
        estimated_costs: Dict[str, gateway_pb2.EstimatedCost],
        weight_clauses: Dict[int, int]
) -> Generator[Tuple[str, gateway_pb2.EstimatedCost], None, None]:
    
    def __compute_score(peer_id: str, estimated_cost: gateway_pb2.EstimatedCost) -> float:
        priority: int = WEIGHT_CONFIGURATION_FACTOR * max(1, weight_clauses[estimated_cost.comb_resource_selected])  # If the combinational resource clause don't have a cost_weight, it's like equal to 1 cost weight.

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
        peer_erg_gas: int = 0 # TODO  Get from Peer protobuf.
        cost: int = gas_cost * (peer_erg_gas / local_erg_gas)

        reputation: float = 1 if peer_id == 'local' else SOCIALIZATION_FACTOR + compute_reputation(peer_id=peer_id)

        log(f"Computing estimated cost score for peer {peer_id}: priority {priority}, reputation {reputation}, cost {cost} => score {priority * reputation / cost}\n")

        return priority * reputation / cost

    return (
        (_id, estimated_cost) for _id, estimated_cost in
        sorted(
            estimated_costs.items(),
            key=lambda item: __compute_score(item[0], item[1]),
            reverse=True
        )
    )
