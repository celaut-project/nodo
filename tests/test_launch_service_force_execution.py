"""`nodo force_execution` bypass in `launch_service` (issue #234).

`launch_service` normally iterates `execution_balancer`'s cheapest-first
candidates. When the call carries a forced-peer hint (looked up by
`recursion_guard_token`, see `SQLConnection.set_forced_execution_peer`), it
must instead delegate straight to that one peer via `_force_delegate` --
skipping the balancer entirely, with no fallback if the forced attempt fails.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.gateway.launcher import launch_service as launch_service_mod
    from protos import celaut_pb2 as celaut
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    launch_service_mod = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ForceDelegateTests(unittest.TestCase):
    def setUp(self):
        self.service = celaut.Service()
        self.metadata = celaut.Metadata()
        self.configuration = celaut.Configuration()
        self.cost = celaut.EstimatedCost()
        self.cost.cost.n = "100"

    def _call(self):
        return launch_service_mod._force_delegate(
            forced_peer="peer-a",
            service=self.service,
            service_id="svc-1",
            metadata=self.metadata,
            configuration=self.configuration,
            father_id="dev-client-1",
            father_ip="10.0.0.1",
            recursion_guard_token="tok-1",
        )

    def test_an_unconnected_peer_fails_fast_before_any_gas_is_touched(self):
        with patch.object(launch_service_mod.sc, "peer_exists", return_value=False), patch.object(
            launch_service_mod, "estimate_cost_on_peer"
        ) as mock_estimate, patch.object(launch_service_mod, "spend_mu") as mock_spend, patch.object(
            launch_service_mod, "delegate_execution"
        ) as mock_delegate:
            with self.assertRaises(Exception):
                self._call()

        mock_estimate.assert_not_called()
        mock_spend.assert_not_called()
        mock_delegate.assert_not_called()

    def test_a_colocation_required_service_refuses_to_delegate(self):
        with patch.object(launch_service_mod.sc, "peer_exists", return_value=True), patch.object(
            launch_service_mod, "service_requires_parent_colocation", return_value=True
        ), patch.object(launch_service_mod, "delegate_execution") as mock_delegate:
            with self.assertRaises(Exception):
                self._call()

        mock_delegate.assert_not_called()

    def test_no_cost_estimate_from_the_peer_raises_without_spending_gas(self):
        with patch.object(launch_service_mod.sc, "peer_exists", return_value=True), patch.object(
            launch_service_mod, "service_requires_parent_colocation", return_value=False
        ), patch.object(
            launch_service_mod, "estimate_cost_on_peer", return_value=None
        ), patch.object(launch_service_mod, "spend_mu") as mock_spend:
            with self.assertRaises(Exception):
                self._call()

        mock_spend.assert_not_called()

    def test_a_failed_gas_spend_raises_without_delegating(self):
        with patch.object(launch_service_mod.sc, "peer_exists", return_value=True), patch.object(
            launch_service_mod, "service_requires_parent_colocation", return_value=False
        ), patch.object(
            launch_service_mod, "estimate_cost_on_peer", return_value=self.cost
        ), patch.object(launch_service_mod, "spend_mu", return_value=False), patch.object(
            launch_service_mod, "delegate_execution"
        ) as mock_delegate:
            with self.assertRaises(Exception):
                self._call()

        mock_delegate.assert_not_called()

    def test_the_happy_path_delegates_straight_to_the_forced_peer(self):
        instance = celaut.ServiceInstance()
        with patch.object(launch_service_mod.sc, "peer_exists", return_value=True), patch.object(
            launch_service_mod, "service_requires_parent_colocation", return_value=False
        ), patch.object(
            launch_service_mod, "estimate_cost_on_peer", return_value=self.cost
        ), patch.object(launch_service_mod, "spend_mu", return_value=True), patch.object(
            launch_service_mod, "delegate_execution", return_value=instance
        ) as mock_delegate, patch.object(
            launch_service_mod.sc, "internal_instance_exists", return_value=False
        ):
            result = self._call()

        self.assertIs(result, instance)
        mock_delegate.assert_called_once()
        self.assertEqual(mock_delegate.call_args.kwargs["peer"], "peer-a")
        self.assertEqual(mock_delegate.call_args.kwargs["service_id"], "svc-1")
        self.assertEqual(mock_delegate.call_args.kwargs["recursion_guard_token"], "tok-1")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchServiceDispatchTests(unittest.TestCase):
    """`launch_service` itself: does it consult the hint and route correctly?"""

    def test_a_forced_peer_hint_skips_the_balancer_entirely(self):
        service = celaut.Service()
        metadata = celaut.Metadata()
        instance = celaut.ServiceInstance()

        with patch.object(
            launch_service_mod.sc, "pop_forced_execution_peer", return_value="peer-a"
        ), patch.object(
            launch_service_mod, "_force_delegate", return_value=instance
        ) as mock_force_delegate, patch.object(
            launch_service_mod, "execution_balancer"
        ) as mock_balancer:
            result = launch_service_mod.launch_service(
                service=service,
                metadata=metadata,
                father_ip="10.0.0.1",
                father_id="dev-client-1",
                service_id="svc-1",
                recursion_guard_token="tok-1",
            )

        self.assertIs(result, instance)
        mock_force_delegate.assert_called_once()
        self.assertEqual(mock_force_delegate.call_args.kwargs["forced_peer"], "peer-a")
        mock_balancer.assert_not_called()

    def test_no_hint_falls_through_to_the_normal_balancer_path(self):
        service = celaut.Service()
        metadata = celaut.Metadata()

        with patch.object(
            launch_service_mod.sc, "pop_forced_execution_peer", return_value=None
        ), patch.object(
            launch_service_mod, "_force_delegate"
        ) as mock_force_delegate, patch.object(
            launch_service_mod, "execution_balancer", return_value=iter([])
        ) as mock_balancer:
            # No candidates and no forced hint -> the pre-existing failure path.
            with self.assertRaises(Exception):
                launch_service_mod.launch_service(
                    service=service,
                    metadata=metadata,
                    father_ip="10.0.0.1",
                    father_id="dev-client-1",
                    service_id="svc-1",
                    recursion_guard_token="tok-1",
                )

        mock_force_delegate.assert_not_called()
        mock_balancer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
