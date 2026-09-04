"""``estimate_cost_on_peer`` (issue #234): the single-peer half of what
``execution_balancer`` already did inline in its comparison loop, factored out
so ``force_execution``'s bypass can ask exactly one peer for its cost without
comparing it to anyone else's.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    import grpc
    from protos import celaut_pb2 as celaut
    from src.balancers.execution_balancer import execution_balancer as balancer_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    balancer_mod = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EstimateCostOnPeerTests(unittest.TestCase):
    def setUp(self):
        self.resources = celaut.Service.Container.Resources()
        self.metadata = celaut.Metadata()
        self.configuration = celaut.Configuration()
        self.matching_payment_system = patch.object(
            balancer_mod, "matching_payment_system", return_value=object()
        )
        self.configuration_for_peer = patch.object(
            balancer_mod,
            "configuration_for_peer",
            side_effect=lambda config, **_: config,
        )
        self.estimated_cost_for_local = patch.object(
            balancer_mod,
            "estimated_cost_for_local",
            side_effect=lambda estimate, **_: estimate,
        )
        self.payment_system = self.matching_payment_system.start().return_value
        self.for_peer = self.configuration_for_peer.start()
        self.for_local = self.estimated_cost_for_local.start()
        self.addCleanup(self.matching_payment_system.stop)
        self.addCleanup(self.configuration_for_peer.stop)
        self.addCleanup(self.estimated_cost_for_local.stop)

    def test_returns_the_peers_estimate_on_success(self):
        expected = celaut.EstimatedCost()
        expected.cost.n = "42"

        with patch.object(balancer_mod, "peer_channel"), patch.object(
            balancer_mod.celaut_pb2_grpc, "GatewayStub"
        ), patch.object(
            balancer_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(
            balancer_mod.bee, "client_grpc", return_value=iter([expected])
        ):
            result = balancer_mod.estimate_cost_on_peer(
                peer_id="peer-a",
                resources=self.resources,
                metadata=self.metadata,
                configuration=self.configuration,
            )

        self.assertIs(result, expected)
        self.for_peer.assert_called_once_with(
            self.configuration, payment_system=self.payment_system
        )
        self.for_local.assert_called_once_with(
            expected, payment_system=self.payment_system
        )

    def test_returns_none_instead_of_raising_when_the_peer_is_unreachable(self):
        with patch.object(balancer_mod, "peer_channel"), patch.object(
            balancer_mod.celaut_pb2_grpc, "GatewayStub"
        ), patch.object(
            balancer_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(
            balancer_mod.bee, "client_grpc", side_effect=RuntimeError("unreachable")
        ):
            result = balancer_mod.estimate_cost_on_peer(
                peer_id="peer-a",
                resources=self.resources,
                metadata=self.metadata,
                configuration=self.configuration,
            )

        self.assertIsNone(result)

    def test_uses_start_service_timeout_when_full_payload_is_sent_for_costing(self):
        with patch.object(balancer_mod, "SEND_ONLY_HASHES_ASKING_COST", False), patch.object(
            balancer_mod, "START_SERVICE_ON_PEER_TIMEOUT", 120
        ), patch.object(balancer_mod, "peer_channel"), patch.object(
            balancer_mod.celaut_pb2_grpc, "GatewayStub"
        ), patch.object(
            balancer_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(
            balancer_mod.bee, "client_grpc", side_effect=RuntimeError("stop after kwargs")
        ) as mock_client:
            result = balancer_mod.estimate_cost_on_peer(
                peer_id="peer-a",
                resources=self.resources,
                metadata=self.metadata,
                configuration=self.configuration,
            )

        self.assertIsNone(result)
        self.assertEqual(mock_client.call_args.kwargs["timeout"], 120)

    def test_uses_external_cost_timeout_for_hash_only_costing(self):
        with patch.object(balancer_mod, "SEND_ONLY_HASHES_ASKING_COST", True), patch.object(
            balancer_mod, "EXTERNAL_COST_TIMEOUT", 10
        ), patch.object(balancer_mod, "peer_channel"), patch.object(
            balancer_mod.celaut_pb2_grpc, "GatewayStub"
        ), patch.object(
            balancer_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(
            balancer_mod.bee, "client_grpc", side_effect=RuntimeError("stop after kwargs")
        ) as mock_client:
            result = balancer_mod.estimate_cost_on_peer(
                peer_id="peer-a",
                resources=self.resources,
                metadata=self.metadata,
                configuration=self.configuration,
            )

        self.assertIsNone(result)
        self.assertEqual(mock_client.call_args.kwargs["timeout"], 10)

    def test_logs_grpc_timeouts_without_claiming_the_service_may_be_missing(self):
        class DeadlineExceeded(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.DEADLINE_EXCEEDED

        with patch.object(balancer_mod, "peer_channel"), patch.object(
            balancer_mod.celaut_pb2_grpc, "GatewayStub"
        ), patch.object(
            balancer_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(
            balancer_mod.bee, "client_grpc", side_effect=DeadlineExceeded("deadline")
        ), patch.object(balancer_mod.log, "LOGGER") as mock_log:
            result = balancer_mod.estimate_cost_on_peer(
                peer_id="peer-a",
                resources=self.resources,
                metadata=self.metadata,
                configuration=self.configuration,
            )

        self.assertIsNone(result)
        logged = mock_log.call_args.args[0]
        self.assertIn("Timeout taking the cost for peer-a", logged)
        self.assertNotIn("maybe it doesn't have the service", logged)


if __name__ == "__main__":
    unittest.main()
