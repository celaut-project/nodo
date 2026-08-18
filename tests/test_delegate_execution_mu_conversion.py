"""Delegation must cross the local/peer MU boundary exactly once.

The configuration is the only thing that crosses here. The cost does not:
`balance_on_other_peer` already answers in our MU, so both sides of the
balance check are local and converting one of them would break it.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.gateway.launcher.delegate_execution import delegate_execution as delegate_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    delegate_mod = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegateExecutionMuConversionTests(unittest.TestCase):
    def test_translates_the_configuration_and_checks_the_balance_in_local_mu(self):
        local_config = celaut.Configuration()
        local_config.initial_mu.n = "1000000"
        peer_config = celaut.Configuration()
        peer_config.initial_mu.n = "2000000"
        instance = celaut.ServiceInstance(token="peer-token")
        payment_system = SimpleNamespace(
            local_mu_per_unit=1_000_000_000,
            peer_mu_per_unit=2_000_000_000,
        )

        with patch.object(
            delegate_mod, "matching_payment_system", return_value=payment_system
        ), patch.object(
            delegate_mod, "configuration_for_peer", return_value=peer_config
        ) as convert_config, patch.object(
            delegate_mod, "balance_on_other_peer", return_value=1_000_001
        ) as balance, patch.object(
            delegate_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(
            delegate_mod.utils, "generate_uris_by_peer_id", return_value=iter(["peer:5000"])
        ), patch.object(delegate_mod, "peer_channel"), patch.object(
            delegate_mod.celaut_pb2_grpc, "GatewayStub"
        ), patch.object(
            delegate_mod.bee, "client_grpc", return_value=iter([instance])
        ), patch.object(
            delegate_mod.delegated_endpoints, "should_tunnel", return_value=False
        ), patch.object(delegate_mod, "SQLConnection") as sql_connection:
            result = delegate_mod.delegate_execution(
                service_id="service-a",
                peer="peer-a",
                father_id="client-a",
                cost=1_000_000,
                metadata=celaut.Metadata(),
                config=local_config,
                recursion_guard_token="token-a",
                refund_container=[],
            )

        self.assertIs(result, instance)
        convert_config.assert_called_once_with(local_config, payment_system=payment_system)
        balance.assert_called_with(peer_id="peer-a")
        sql_connection.return_value.add_delegated_instance.assert_called_once()

    def test_refuses_when_the_local_balance_does_not_cover_the_local_cost(self):
        # A balance of exactly the cost is not enough (the check is `<=`), and both
        # figures are ours: a peer-scaled balance compared against a local cost is
        # the bug this test pins down.
        payment_system = SimpleNamespace(
            local_mu_per_unit=1_000_000_000, peer_mu_per_unit=2_000_000_000
        )
        with patch.object(
            delegate_mod, "matching_payment_system", return_value=payment_system
        ), patch.object(
            delegate_mod, "configuration_for_peer", return_value=celaut.Configuration()
        ), patch.object(
            delegate_mod, "balance_on_other_peer", return_value=1_000_000
        ), patch.object(delegate_mod, "SQLConnection"), patch.object(
            delegate_mod.log, "LOGGER"
        ):
            with self.assertRaises(Exception):
                delegate_mod.delegate_execution(
                    service_id="service-a",
                    peer="peer-a",
                    father_id="client-a",
                    cost=1_000_000,
                    metadata=celaut.Metadata(),
                    config=celaut.Configuration(),
                    recursion_guard_token="token-a",
                    refund_container=[],
                )


if __name__ == "__main__":
    unittest.main()
