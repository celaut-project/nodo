from typing import Dict, Tuple, Generator

from protos import gateway_pb2
from src.utils.cost_functions.general_cost_functions import normalized_maintain_cost
from src.utils.cost_functions.variance_cost_normalization import variance_cost_normalization
from src.utils.env import WEIGHT_CONFIGURATION_FACTOR
from src.utils.utils import from_gas_amount


def estimated_cost_sorter(
        estimated_costs: Dict[str, gateway_pb2.EstimatedCost],
        weight_clauses: Dict[int, int]
) -> Generator[Tuple[str, gateway_pb2.EstimatedCost], None, None]:
    def __compute_score(estimated_cost: gateway_pb2.EstimatedCost) -> float:
        return WEIGHT_CONFIGURATION_FACTOR * weight_clauses[estimated_cost.comb_resource_selected] / sum([
            variance_cost_normalization(
                from_gas_amount(estimated_cost.cost),
                estimated_cost.variance
            ),
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
            )
        ])

    return ((_id, estimated_cost) for _id, estimated_cost in
            sorted(estimated_costs.items(),
                   key=lambda item: __compute_score(item[1]),
                   reverse=True
                   )
            )
