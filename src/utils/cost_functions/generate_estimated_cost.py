from typing import Optional, Callable

from protos import celaut_pb2 as celaut, gateway_pb2
from src.balancers.configuration_balancer.configuration_balancer import configuration_balancer


def generate_estimated_cost(
        metadata: celaut.Metadata,
        initial_gas_amount: int,
        config: Optional[gateway_pb2.Configuration]
) -> gateway_pb2.EstimatedCost:
    return configuration_balancer(
        clauses=dict(config.resources.clause),
        metadata=metadata,
        initial_gas_amount=initial_gas_amount
    )[1]
