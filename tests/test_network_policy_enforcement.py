"""Where the network policy is actually applied (issue #280).

The policy itself is tested in ``test_network_policy.py``. What is under test here
is that each of its three enforcement points consults it, and consults it early
enough to matter: before the balancer in the launcher, before a price is quoted to
a peer, and before the virtualizer opens anything for a guest.

Each point is guarded on its own import: the launcher and the virtualizer pull in
grpc/bee_rpc and the node's runtime tree, which a minimal checkout does not have.
"""
import unittest
from unittest.mock import patch

POLICY_IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    # The three enforcement points live in modules that read config.yaml at import
    # time, so the example config is loaded before any of them is imported.
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.utils import network_policy as np
except Exception as import_exc:  # pragma: no cover - environment-dependent
    POLICY_IMPORT_ERROR = import_exc
    celaut = None
    np = None

LAUNCHER_IMPORT_ERROR = None
try:
    from src.gateway.launcher import launch_service as launch_service_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    LAUNCHER_IMPORT_ERROR = import_exc
    launch_service_mod = None

COST_IMPORT_ERROR = None
try:
    from src.gateway.iterables import estimated_cost_iterable as cost_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    COST_IMPORT_ERROR = import_exc
    cost_mod = None

VIRTUALIZER_IMPORT_ERROR = None
try:
    from src.virtualizers.microvm import rootfs as microvm_rootfs
except Exception as import_exc:  # pragma: no cover - environment-dependent
    VIRTUALIZER_IMPORT_ERROR = import_exc
    microvm_rootfs = None

SERVE_IMPORT_ERROR = None
try:
    from src import serve as serve_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    SERVE_IMPORT_ERROR = import_exc
    serve_mod = None


def _service(*tag_groups):
    service = celaut.Service() if celaut else None
    for tags in tag_groups:
        service.network.append(celaut.Service.Network(tags=list(tags)))
    return service


def _policy(**kwargs):
    """Force ``from_config`` to answer with a policy of our own, config or no config."""
    return patch.object(
        np.NetworkPolicy, "from_config", classmethod(lambda cls, env_manager=None: np.NetworkPolicy(**kwargs))
    )


@unittest.skipIf(
    POLICY_IMPORT_ERROR is not None or LAUNCHER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {POLICY_IMPORT_ERROR or LAUNCHER_IMPORT_ERROR}",
)
class LauncherTests(unittest.TestCase):
    """The rejection has to happen before the node can spend anything on the service."""

    def _launch(self, service, token):
        # A funded configuration, so the launch does not price a starting balance on
        # the way in: what is under test is the order of the checks, not the
        # pricing stack `default_initial_balance` would pull in.
        configuration = celaut.Configuration()
        configuration.initial_mu.n = "1000"

        return launch_service_mod.launch_service(
            service=service,
            metadata=celaut.Metadata(),
            father_ip="10.0.0.1",
            father_id="dev-client-1",
            service_id="svc-1",
            configuration=configuration,
            recursion_guard_token=token,
        )

    def test_a_blacklisted_declaration_never_reaches_the_balancer(self):
        with _policy(blacklist=["*google.com"]), patch.object(
            launch_service_mod, "execution_balancer"
        ) as balancer, patch.object(
            launch_service_mod, "_detect_local_preflight_failure"
        ) as preflight:
            with self.assertRaises(np.NetworkPolicyRejection) as ctx:
                self._launch(_service(["maps.google.com"]), "tok-policy-1")

        balancer.assert_not_called()
        # Not even the local preflight ran: nothing about this service is priced.
        preflight.assert_not_called()
        self.assertIn("service svc-1", str(ctx.exception))
        self.assertIn("service_networks.blacklist", str(ctx.exception))

    def test_the_force_execution_bypass_does_not_bypass_the_policy(self):
        # force_execution overrides peer *selection*, not what this node is willing
        # to have reached on its behalf.
        with _policy(blacklist=["*"]), patch.object(
            launch_service_mod.sc, "pop_forced_execution_peer", return_value="peer-a"
        ), patch.object(launch_service_mod, "_force_delegate") as force_delegate:
            with self.assertRaises(np.NetworkPolicyRejection):
                self._launch(_service(["dns:local"]), "tok-policy-2")

        force_delegate.assert_not_called()

    def test_an_allowed_declaration_falls_through_to_the_normal_path(self):
        with _policy(whitelist=["dns:*"]), patch.object(
            launch_service_mod.sc, "pop_forced_execution_peer", return_value=None
        ), patch.object(
            launch_service_mod, "_detect_local_preflight_failure", return_value=None
        ), patch.object(
            launch_service_mod, "execution_balancer", return_value=iter([])
        ) as balancer:
            # No candidates -> the pre-existing "nothing could run it" failure, which
            # is not a policy rejection.
            with self.assertRaises(Exception) as ctx:
                self._launch(_service(["dns:local"]), "tok-policy-3")

        self.assertNotIsInstance(ctx.exception, np.NetworkPolicyRejection)
        balancer.assert_called_once()


@unittest.skipIf(
    POLICY_IMPORT_ERROR is not None or COST_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {POLICY_IMPORT_ERROR or COST_IMPORT_ERROR}",
)
class CostQuoteTests(unittest.TestCase):
    """A price is an offer; this node does not offer one it would then refuse."""

    def _iterable(self, token):
        it = cost_mod.GetServiceEstimatedCostIterable.__new__(
            cost_mod.GetServiceEstimatedCostIterable
        )
        it.configuration = None
        it.service_hash = "abc123"
        it.metadata = celaut.Metadata()
        it.recursion_guard_token = token
        return it

    def test_a_rejected_service_is_not_quoted(self):
        with _policy(blacklist=["*google.com"]), patch.object(
            cost_mod, "read_service_from_disk", return_value=_service(["maps.google.com"])
        ), patch.object(cost_mod, "generate_estimated_cost") as quote:
            with self.assertRaises(np.NetworkPolicyRejection):
                list(self._iterable("tok-cost-1").generate())

        quote.assert_not_called()

    def test_an_allowed_service_is_quoted(self):
        with _policy(whitelist=["dns:*"]), patch.object(
            cost_mod, "read_service_from_disk", return_value=_service(["dns:local"])
        ), patch.object(
            cost_mod, "default_initial_balance", return_value=1
        ), patch.object(
            cost_mod, "generate_estimated_cost", return_value=celaut.EstimatedCost()
        ) as quote, patch.object(cost_mod.bee, "serialize_to_buffer", return_value=iter([])):
            list(self._iterable("tok-cost-2").generate())

        quote.assert_called_once()


@unittest.skipIf(
    POLICY_IMPORT_ERROR is not None or VIRTUALIZER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {POLICY_IMPORT_ERROR or VIRTUALIZER_IMPORT_ERROR}",
)
class VirtualizerTests(unittest.TestCase):
    """Defence in depth: judged on the set that survived the ancestor chain."""

    def test_a_forbidden_network_aborts_instead_of_being_dropped(self):
        with _policy(blacklist=["*google.com"]), patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            with self.assertRaises(np.NetworkPolicyRejection):
                microvm_rootfs.build_network_resolution(
                    service=_service(["maps.google.com"]), father_id="father-1"
                )

        resolve.assert_not_called()

    def test_what_the_ancestor_chain_removed_is_no_longer_judged(self):
        # The blacklisted network never reaches the guest, so there is nothing left
        # for the policy to refuse at this point.
        with _policy(blacklist=["*google.com"]), patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=True
        ), patch.object(
            microvm_rootfs, "filter_networks_with_ancestors",
            return_value=[celaut.Service.Network(tags=["dns:local"])],
        ), patch.object(microvm_rootfs, "resolve_network", return_value=[]) as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=_service(["maps.google.com"], ["dns:local"]), father_id="father-1"
            )

        self.assertEqual([list(r.tags) for r in resolution], [["dns:local"]])
        resolve.assert_called_once()


@unittest.skipIf(
    POLICY_IMPORT_ERROR is not None or SERVE_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {POLICY_IMPORT_ERROR or SERVE_IMPORT_ERROR}",
)
class StartupReportTests(unittest.TestCase):
    def test_a_policy_the_node_cannot_parse_stops_it_at_start(self):
        # Not at the first launch, and never as "no restrictions".
        with patch.object(
            serve_mod.NetworkPolicy, "from_config",
            side_effect=np.NetworkPolicyConfigError("blacklist must be a list"),
        ):
            with self.assertRaises(SystemExit):
                serve_mod._report_network_policy()

    def test_the_policy_in_force_is_logged(self):
        with _policy(blacklist=["*google.com"]), patch.object(serve_mod.log, "LOGGER") as logger:
            serve_mod._report_network_policy()

        self.assertIn("*google.com", logger.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
