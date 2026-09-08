"""A share that does not exist must fail the launch before anything is spent.

`guest=true` is an execution precondition, so the check belongs ahead of the
balancer loop and ahead of `spend_mu` -- and outside `_detect_local_preflight_failure`,
which reports a *local* failure worth trying another candidate for. There is no
other candidate: the export is materialized from the parent's own rootfs, on the
parent's own node, so a share that does not attach makes the service unrunnable
everywhere.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.gateway.launcher import launch_service as mod
    from src.manager.shares import ShareAuthorizationError
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    mod = celaut = ShareAuthorizationError = None


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SharesPreflightTests(unittest.TestCase):
    def _launch(self, authorize_side_effect):
        """Drive launch_service far enough to reach the shares preflight."""
        service = celaut.Service()
        with patch.object(mod, "authorize_shares", side_effect=authorize_side_effect) as authorize, \
             patch.object(mod, "spend_mu") as spend, \
             patch.object(mod, "execution_balancer") as balancer, \
             patch.object(mod, "local_execution") as local, \
             patch.object(mod, "delegate_execution") as delegate, \
             patch.object(mod, "enforce_network_policy"), \
             patch.object(mod, "evaluate_possible_environment_workloads", return_value=None), \
             patch.object(mod, "_detect_local_preflight_failure", return_value=None), \
             patch.object(mod, "descends_from_dev_client", return_value=True), \
             patch.object(mod.activity_window, "is_open", return_value=True), \
             patch.object(mod.sc, "pop_forced_execution_peer", return_value=None), \
             patch.object(mod, "get_arch_tag", return_value="amd64"), \
             patch.object(mod, "default_initial_balance", return_value=1):
            balancer.return_value = iter([])
            with self.assertRaises(Exception) as ctx:
                mod.launch_service(
                    service=service,
                    metadata=celaut.Metadata(),
                    configuration=celaut.Configuration(),
                    father_id="vm-parent",
                    father_ip="10.0.0.1",
                    service_id="svc-child",
                )
            return ctx.exception, authorize, spend, balancer, local, delegate

    def test_an_ungranted_share_stops_the_launch_before_the_balancer_or_any_charge(self):
        error, authorize, spend, balancer, local, delegate = self._launch(
            ShareAuthorizationError("its parent does not export 'hdfs-data' at all")
        )
        authorize.assert_called_once()
        # Nothing was quoted, charged, delegated or run.
        spend.assert_not_called()
        balancer.assert_not_called()
        local.assert_not_called()
        delegate.assert_not_called()
        # The reason reaches the client, not just the log.
        self.assertIn("hdfs-data", str(error))
        self.assertIn("svc-child", str(error))

    def test_a_granted_share_does_not_stop_the_launch_here(self):
        # With nothing refused the launch proceeds and fails later, on the empty
        # balancer -- i.e. the preflight is not what ended it.
        error, authorize, spend, _balancer, _local, _delegate = self._launch(None)
        authorize.assert_called_once()
        spend.assert_not_called()
        self.assertNotIn("does not export", str(error))


if __name__ == "__main__":
    unittest.main()
