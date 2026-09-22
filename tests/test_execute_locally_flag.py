"""`network.EXECUTE_LOCALLY: false` must leave the peers as the only candidates.

The mirror image of `network.DELEGATE_EXECUTION`, for a node meant to orchestrate
rather than compute. It has to skip the local quote rather than filter it out
afterwards: `generate_estimated_cost` resolves the service's architecture, reads its
manifest off disk and applies this node's whole pricing policy, and all of that would
be spent producing a candidate that could never be selected.
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
class ExecuteLocallyFlagTests(unittest.TestCase):

    def _run(self, execute_locally: bool, delegate: bool = True):
        """Drive the real balancer with one reachable peer available.

        The sorter is stubbed to yield the candidates unranked: how they are ordered
        is its business (and it reads peer reputation from the database), while these
        flags' job is only which candidates exist to be ranked at all.
        """
        settings = {
            "network.EXECUTE_LOCALLY": execute_locally,
            "network.DELEGATE_EXECUTION": delegate,
        }
        with mock.patch.object(balancer_module.env_manager, "get",
                               side_effect=lambda key, default=None:
                                   settings.get(key, default)), \
                mock.patch.object(balancer_module, "generate_estimated_cost",
                                  return_value=celaut_pb2.EstimatedCost()) as local_cost, \
                mock.patch.object(balancer_module, "peers_id_iterator", return_value=iter(["peer-1"])), \
                mock.patch.object(balancer_module, "estimated_cost_sorter",
                                  side_effect=lambda estimated_costs: iter(estimated_costs.items())), \
                mock.patch.object(balancer_module, "estimate_cost_on_peer",
                                  return_value=celaut_pb2.EstimatedCost()):
            candidates = [peer for peer, _ in balancer_module.execution_balancer(
                service_id="s1",
                resources=celaut_pb2.Service.Container.Resources(),
                metadata=celaut_pb2.Metadata(),
                configuration=celaut_pb2.Configuration(),
            )]
        return candidates, local_cost

    def test_this_node_is_a_candidate_while_local_execution_is_on(self):
        # Guards the test itself: without it the "off" case could pass for any reason.
        candidates, local_cost = self._run(execute_locally=True)
        self.assertIn("local", candidates)
        local_cost.assert_called_once()

    def test_this_node_is_neither_priced_nor_offered_while_local_execution_is_off(self):
        candidates, local_cost = self._run(execute_locally=False)
        self.assertEqual(candidates, ["peer-1"])
        # Not merely filtered out afterwards: pricing it at all is the cost this
        # flag exists to avoid.
        local_cost.assert_not_called()

    def test_the_two_flags_are_independent(self):
        """Off in both directions is a node with no candidates at all.

        A coherent thing to configure -- a node that is only a wallet and a peer
        directory -- and not something either flag should quietly override in the
        other's favour.
        """
        candidates, local_cost = self._run(execute_locally=False, delegate=False)
        self.assertEqual(candidates, [])
        local_cost.assert_not_called()

    def test_local_execution_is_on_unless_it_is_turned_off(self):
        """A config written before this key existed must keep running services.

        The default reaches the balancer through `env_manager.get`'s own default
        argument, so a node that has never heard of this setting behaves exactly as
        it did before it was added.
        """
        with mock.patch.object(balancer_module.env_manager, "get",
                               side_effect=lambda key, default=None: default), \
                mock.patch.object(balancer_module, "generate_estimated_cost",
                                  return_value=celaut_pb2.EstimatedCost()), \
                mock.patch.object(balancer_module, "peers_id_iterator", return_value=iter([])), \
                mock.patch.object(balancer_module, "estimated_cost_sorter",
                                  side_effect=lambda estimated_costs: iter(estimated_costs.items())):
            candidates = [peer for peer, _ in balancer_module.execution_balancer(
                service_id="s1",
                resources=celaut_pb2.Service.Container.Resources(),
                metadata=celaut_pb2.Metadata(),
                configuration=celaut_pb2.Configuration(),
            )]

        self.assertEqual(candidates, ["local"])


if __name__ == "__main__":
    unittest.main()
