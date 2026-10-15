"""A missing virtiofsd must stop a launch before anything is charged or built (#478).

A service with a `guest` directory can only run on this node, so the launch fails
at once. A service that only exports can still run on a peer, so this node stops
being a candidate and the peers are still tried. A service with no shared
directories must not probe for the daemon at all.
"""
import unittest
from types import SimpleNamespace
from contextlib import ExitStack
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.gateway.launcher import launch_service as mod
    from src.utils.shared_filesystems import SharedDir
    from src.virtualizers.microvm.virtiofsd import VirtiofsdUnavailable
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    mod = celaut = SharedDir = VirtiofsdUnavailable = None


def _dir(path, *, shared=False, guest=False):
    return SharedDir(path=path, shared=shared, guest=guest, access="rw", tag="", env="")


MISSING = "this service declares shared directories, which need the Rust virtiofsd on this node. No virtiofsd."


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VirtiofsdPreflightTests(unittest.TestCase):
    def _launch(self, declarations, require_side_effect, candidates=()):
        cost = SimpleNamespace(cost=SimpleNamespace())
        with ExitStack() as stack:
            def patched(target, name, **kwargs):
                return stack.enter_context(patch.object(target, name, **kwargs))

            patched(mod, "declarations_for_service", return_value=declarations)
            require = patched(mod.virtiofsd, "require_usable", side_effect=require_side_effect)
            patched(mod, "service_requires_parent_colocation",
                    return_value=any(d.guest for d in declarations))
            patched(mod, "authorize_shares", return_value=[])
            spend = patched(mod, "spend_mu", return_value=True)
            balancer = patched(mod, "execution_balancer")
            local = patched(mod, "local_execution")
            delegate = patched(mod, "delegate_execution", return_value=SimpleNamespace(instance=None))
            patched(mod, "from_amount", return_value=1)
            patched(mod, "enforce_network_policy")
            patched(mod, "evaluate_possible_environment_workloads", return_value=None)
            patched(mod, "_detect_local_preflight_failure", return_value=None)
            patched(mod, "descends_from_dev_client", return_value=True)
            patched(mod.activity_window, "is_open", return_value=True)
            patched(mod.demand_history, "record_admission")
            patched(mod.demand_history, "record_refusal")
            patched(mod.sc, "pop_forced_execution_peer", return_value=None)
            patched(mod.sc, "internal_instance_exists", return_value=False)
            patched(mod.utils, "get_network_name", return_value="lan")
            patched(mod, "get_arch_tag", return_value="amd64")
            patched(mod, "default_initial_balance", return_value=1)
            balancer.return_value = iter([(peer, cost) for peer in candidates])
            error = None
            try:
                mod.launch_service(
                    service=celaut.Service(),
                    metadata=celaut.Metadata(),
                    configuration=celaut.Configuration(),
                    father_id="vm-parent",
                    father_ip="10.0.0.1",
                    service_id="svc",
                )
            except Exception as e:
                error = e
            return SimpleNamespace(
                error=error, require=require, spend=spend, balancer=balancer,
                local=local, delegate=delegate,
            )

    def test_guest_service_fails_before_the_balancer_or_any_charge(self):
        r = self._launch([_dir("/data", guest=True)], VirtiofsdUnavailable(MISSING),
                         candidates=["local"])
        r.require.assert_called_once()
        self.assertIsNotNone(r.error)
        self.assertIn("Unable to launch service svc", str(r.error))
        self.assertIn("Rust virtiofsd", str(r.error))
        r.balancer.assert_not_called()
        r.spend.assert_not_called()
        r.local.assert_not_called()
        r.delegate.assert_not_called()

    def test_exporter_skips_this_node_but_a_peer_can_still_run_it(self):
        r = self._launch([_dir("/data", shared=True)], VirtiofsdUnavailable(MISSING),
                         candidates=["local", "peer-a"])
        self.assertIsNone(r.error)
        r.local.assert_not_called()
        # Charged once, for the peer, and never for the local attempt.
        r.spend.assert_called_once()
        r.delegate.assert_called_once()
        self.assertEqual(r.delegate.call_args.kwargs["peer"], "peer-a")

    def test_exporter_with_only_this_node_fails_with_the_reason_and_no_charge(self):
        r = self._launch([_dir("/data", shared=True)], VirtiofsdUnavailable(MISSING),
                         candidates=["local"])
        self.assertIsNotNone(r.error)
        self.assertIn("local: this service declares shared directories", str(r.error))
        r.spend.assert_not_called()
        r.local.assert_not_called()

    def test_usable_virtiofsd_does_not_change_the_launch(self):
        r = self._launch([_dir("/data", guest=True)], None, candidates=["local"])
        r.require.assert_called_once()
        self.assertIsNone(r.error)
        r.spend.assert_called_once()
        r.local.assert_called_once()

    def test_service_without_shares_does_not_probe_virtiofsd(self):
        r = self._launch([], VirtiofsdUnavailable(MISSING), candidates=["local"])
        r.require.assert_not_called()
        self.assertIsNone(r.error)
        r.local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
