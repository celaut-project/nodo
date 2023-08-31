from typing import Dict, Tuple, List

from protos import gateway_pb2
from src.balancers.resource_balancer.resource_balancer import ClauseResource
from src.balancers.resource_balancer.variance_cost_normalization import variance_cost_normalization
from src.utils.utils import from_gas_amount
from src.utils.logger import LOGGER


class PeerCostList:
    # Sorts the list from the element with the smallest weight to the element with the largest weight.

    def __init__(self, config) -> None:
        self.peer_costs: Dict[str, int] = {}  # peer_id : cost
        self.peer_resources: Dict[str, int] = {}  # peer_id : clause id
        self.clauses: Dict[int, ClauseResource] = dict(config.resources.clause)

    def add_elem(self, estim_cost: gateway_pb2.EstimatedCost, elem: str = 'local') -> None:
        LOGGER('    adding elem ' + elem + ' with weight ' + str(estim_cost.cost))

        if estim_cost.comb_resource_selected not in self.clauses:
            raise Exception(f"Invalid selected clause {estim_cost.comb_resource_selected}.")

        self.peer_costs.update({
            elem: variance_cost_normalization(cost=from_gas_amount(estim_cost.cost), variance=estim_cost.variance)
        })

        self.peer_resources.update({elem: estim_cost.comb_resource_selected})

    def get(self) -> List[Tuple[str, int, int]]:
        return [(k, v, self.peer_resources[k]) for k, v in
                sorted(self.peer_costs.items(),
                       key=lambda item: self.clauses[self.peer_resources[item[0]]].cost_weight / item[1],
                       reverse=True
                       )
                ]