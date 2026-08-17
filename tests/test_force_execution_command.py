"""`nodo force_execution <peer_id> <service>` (issue #234).

Mirrors the mocking conventions in `test_execute_command.py`: the gRPC/gateway
plumbing itself is shared (`execute.launch_via_gateway`) and already covered
there, so these tests focus on what's specific to `force_execution` -- the
fail-fast peer check, and the token-based hint lifecycle around the call.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.commands import force_execution as force_execution_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    force_execution_cmd = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ForceExecutionCommandTests(unittest.TestCase):
    def test_refuses_when_the_peer_is_not_connected(self):
        with patch.object(force_execution_cmd.sc, "peer_exists", return_value=False), patch.object(
            force_execution_cmd, "resolve_service_hash"
        ) as mock_resolve, patch.object(
            force_execution_cmd, "launch_via_gateway"
        ) as mock_launch:
            force_execution_cmd.force_execution(peer_id="peer-a", service="svc")

        mock_resolve.assert_not_called()
        mock_launch.assert_not_called()

    def test_refuses_an_unresolvable_service_after_trying_to_acquire_it(self):
        with patch.object(force_execution_cmd.sc, "peer_exists", return_value=True), patch.object(
            force_execution_cmd, "resolve_service_hash", return_value=""
        ), patch.object(
            force_execution_cmd, "acquire_service", return_value=False
        ), patch.object(force_execution_cmd, "launch_via_gateway") as mock_launch:
            force_execution_cmd.force_execution(peer_id="peer-a", service="svc")

        mock_launch.assert_not_called()

    def test_a_successful_call_stores_then_cleans_up_the_same_token(self):
        response = celaut.ServiceInstance()
        recorded = {}

        def fake_set(token, peer_id):
            recorded["set"] = (token, peer_id)

        def fake_pop(token):
            recorded["pop"] = token
            return None

        with patch.object(force_execution_cmd.sc, "peer_exists", return_value=True), patch.object(
            force_execution_cmd, "resolve_service_hash", return_value="svc-resolved"
        ), patch.object(
            force_execution_cmd.sc, "set_forced_execution_peer", side_effect=fake_set
        ), patch.object(
            force_execution_cmd.sc, "pop_forced_execution_peer", side_effect=fake_pop
        ), patch.object(
            force_execution_cmd, "launch_via_gateway", return_value=response
        ) as mock_launch, patch.object(
            force_execution_cmd, "print_endpoints"
        ) as mock_print:
            force_execution_cmd.force_execution(peer_id="peer-a", service="svc")

        mock_launch.assert_called_once()
        self.assertEqual(recorded["set"][1], "peer-a")
        # The cleanup pop must use the exact same token the hint was stored under.
        self.assertEqual(recorded["pop"], recorded["set"][0])
        mock_print.assert_called_once_with(response)

    def test_forced_generator_does_not_forward_local_funding_as_initial_mu(self):
        with patch.object(force_execution_cmd, "get_execute_client", return_value="client-a") as mock_client:
            messages = list(force_execution_cmd._forced_generator(
                _hash="aa" * 32,
                token="forced-token",
                local_client_balance_mu=10**16,
            ))

        mock_client.assert_called_once_with(amount_mu=10**16, external=False)
        configs = [message for message in messages if isinstance(message, celaut.Configuration)]
        self.assertEqual(len(configs), 1)
        self.assertFalse(configs[0].HasField("initial_mu"))

    def test_the_hint_is_cleaned_up_even_when_the_gateway_call_fails(self):
        with patch.object(force_execution_cmd.sc, "peer_exists", return_value=True), patch.object(
            force_execution_cmd, "resolve_service_hash", return_value="svc-resolved"
        ), patch.object(force_execution_cmd.sc, "set_forced_execution_peer"), patch.object(
            force_execution_cmd.sc, "pop_forced_execution_peer"
        ) as mock_pop, patch.object(
            force_execution_cmd, "launch_via_gateway", return_value=None
        ), patch.object(force_execution_cmd, "print_endpoints") as mock_print:
            force_execution_cmd.force_execution(peer_id="peer-a", service="svc")

        mock_pop.assert_called_once()
        mock_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
