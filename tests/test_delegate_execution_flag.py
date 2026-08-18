"""`network.DELEGATE_EXECUTION: false` must leave 'local' as the only candidate.

The balancer polls every known peer for a price before ranking them. A node that will
not delegate has to skip that loop rather than filter its results afterwards: each
round-trip is a `GetServiceEstimatedCost` call to a candidate that could never be
selected, and on an unreachable peer it costs `EXTERNAL_COST_TIMEOUT` seconds to learn
nothing.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.balancers.execution_balancer import execution_balancer as balancer_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    balancer_module = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegateExecutionFlagTests(unittest.TestCase):

    def _run(self, delegate: bool):
        """Drive the real balancer with one reachable peer available.

        The sorter is stubbed to yield the candidates unranked: how they are ordered is
        its business (and it reads peer reputation from the database), while the flag's
        job is only which candidates exist to be ranked at all.
        """
        with mock.patch.object(balancer_module.env_manager, "get",
                               side_effect=lambda key, default=None:
                                   delegate if key == "network.DELEGATE_EXECUTION" else default), \
                mock.patch.object(balancer_module, "generate_estimated_cost",
                                  return_value=celaut_pb2.EstimatedCost()), \
                mock.patch.object(balancer_module, "peers_id_iterator", return_value=iter(["peer-1"])), \
                mock.patch.object(balancer_module, "estimated_cost_sorter",
                                  side_effect=lambda estimated_costs: iter(estimated_costs.items())), \
                mock.patch.object(balancer_module, "estimate_cost_on_peer",
                                  return_value=celaut_pb2.EstimatedCost()) as estimate:
            candidates = [peer for peer, _ in balancer_module.execution_balancer(
                service_id="s1",
                resources=celaut_pb2.Service.Container.Resources(),
                metadata=celaut_pb2.Metadata(),
                configuration=celaut_pb2.Configuration(),
            )]
        return candidates, estimate

    def test_the_peer_is_a_candidate_while_delegation_is_on(self):
        # Guards the test itself: without it the "off" case could pass for any reason.
        candidates, estimate = self._run(delegate=True)
        self.assertIn("peer-1", candidates)
        estimate.assert_called_once()

    def test_no_peer_is_polled_or_offered_while_delegation_is_off(self):
        candidates, estimate = self._run(delegate=False)
        self.assertEqual(candidates, ["local"])
        estimate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
