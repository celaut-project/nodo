from typing import Dict, Tuple, Generator

from protos import gateway_pb2
from src.reputation_system.simple_reputation_feedback import compute_reputation_feedback
from src.utils.cost_functions.general_cost_functions import normalized_maintain_cost
from src.utils.cost_functions.variance_cost_normalization import variance_cost_normalization
from src.utils.env import EnvManager
from src.utils.utils import from_gas_amount

env_manager = EnvManager()
SOCIALIZATION_FACTOR = env_manager.get_env("SOCIALIZATION_FACTOR")
WEIGHT_CONFIGURATION_FACTOR = env_manager.get_env("WEIGHT_CONFIGURATION_FACTOR")
INIT_COST_CONFIGURATION_FACTOR = env_manager.get_env("INIT_COST_CONFIGURATION_FACTOR")
MAINTENANCE_COST_CONFIGURATION_FACTOR = env_manager.get_env("MAINTENANCE_COST_CONFIGURATION_FACTOR")

def estimated_cost_sorter(
        estimated_costs: Dict[str, gateway_pb2.EstimatedCost],
        weight_clauses: Dict[int, int]
) -> Generator[Tuple[str, gateway_pb2.EstimatedCost], None, None]:
    def __compute_score(peer_id: str, estimated_cost: gateway_pb2.EstimatedCost) -> float:
        priority: int = WEIGHT_CONFIGURATION_FACTOR * max(1, weight_clauses[estimated_cost.comb_resource_selected])  # If the combinational resource clause don't have a cost_weight, it's like equal to 1 cost weight.
        cost: int = sum([
            variance_cost_normalization(
                from_gas_amount(estimated_cost.cost),
                estimated_cost.variance
            ) * INIT_COST_CONFIGURATION_FACTOR,
            int(
                sum([
                    variance_cost_normalization(
                        normalized_maintain_cost(
                            from_gas_amount(estimated_cost.min_maintenance_cost),
                            estimated_cost.maintenance_seconds_loop
                        ),
                        estimated_cost.variance
                    ),
                    variance_cost_normalization(
                        normalized_maintain_cost(
                            from_gas_amount(estimated_cost.max_maintenance_cost),
                            estimated_cost.maintenance_seconds_loop
                        ),
                        estimated_cost.variance
                    )
                ]) / 2
            ) * MAINTENANCE_COST_CONFIGURATION_FACTOR
        ])
        reputation: float = 1 if peer_id == 'local' else SOCIALIZATION_FACTOR + compute_reputation_feedback(peer_id=peer_id)

        print(f"\nDebug: For peer {peer_id}: priority {priority}, reputation {reputation}, cost {cost} => score {priority * reputation / cost}\n", flush=True)

        return priority * reputation / cost

    return (
        (_id, estimated_cost) for _id, estimated_cost in
        sorted(
            estimated_costs.items(),
            key=lambda item: __compute_score(item[0], item[1]),
            reverse=True
        )
    )
