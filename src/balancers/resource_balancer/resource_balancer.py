from typing import Dict, Optional

from protos import gateway_pb2
from src.manager.manager import could_ve_this_sysreq


ClauseResource = gateway_pb2.CombinationResources.Clause
#  TODO make from protos.gateway_pb2.CombinationResource.Clause as ClauseResource


def compute_score_resource_clause(clause: ClauseResource) -> int:
    return 0  # TODO add resources cost to the execution cost.


def resource_configuration_balancer(clauses: Dict[int, ClauseResource]) -> int:
    _max_score: int = 0
    _best_clause: Optional[ClauseResource] = None
    for _i, clause in clauses:
        if not could_ve_this_sysreq(clause.max_sysreq):
            continue
        __local_score: int = compute_score_resource_clause(clause)
        if _max_score < __local_score:
            _max_score = __local_score
            _best_clause = clause

    return _best_clause if _best_clause \
        else next((_i for _i, clause in clauses
                   if clause.HasField('max_sysreq') and could_ve_this_sysreq(clause.max_sysreq)), None)
