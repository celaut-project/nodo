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

    def test_returns_the_peers_estimate_on_success(self):
        expected = celaut.EstimatedCost()
        expected.cost.n = "42"

        with patch.object(
            balancer_mod, "generate_uris_by_peer_id", return_value=iter(["10.0.0.1:5000"])
        ), patch.object(balancer_mod.grpc, "insecure_channel"), patch.object(
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

    def test_returns_none_instead_of_raising_when_the_peer_is_unreachable(self):
        with patch.object(
            balancer_mod, "generate_uris_by_peer_id", return_value=iter(["10.0.0.1:5000"])
        ), patch.object(balancer_mod.grpc, "insecure_channel"), patch.object(
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
        ), patch.object(
            balancer_mod, "generate_uris_by_peer_id", return_value=iter(["10.0.0.1:5000"])
        ), patch.object(balancer_mod.grpc, "insecure_channel"), patch.object(
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
        ), patch.object(
            balancer_mod, "generate_uris_by_peer_id", return_value=iter(["10.0.0.1:5000"])
        ), patch.object(balancer_mod.grpc, "insecure_channel"), patch.object(
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

        with patch.object(
            balancer_mod, "generate_uris_by_peer_id", return_value=iter(["10.0.0.1:5000"])
        ), patch.object(balancer_mod.grpc, "insecure_channel"), patch.object(
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
