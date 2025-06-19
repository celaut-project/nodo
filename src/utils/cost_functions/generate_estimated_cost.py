from protos import celaut_pb2 as celaut, celaut_pb2
from src.balancers.configuration_balancer.configuration_balancer import configuration_balancer
from src.utils.utils import from_gas_amount


def generate_estimated_cost(
        metadata: celaut.Metadata,
        config: celaut_pb2.Configuration,
        resources
) -> celaut_pb2.EstimatedCost:
    
    if not resources or not resources.clause:
        raise Exception("Can't generate estimated cost without any configuration defined.")

    return configuration_balancer(
        clauses=dict(resources.clause),
        metadata=metadata,
        initial_gas_amount=from_gas_amount(config.initial_gas_amount)
    )[1]
