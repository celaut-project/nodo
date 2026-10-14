"""Hidden mode and the networks a service declares (phase 3 of #539).

A hidden node does not run a service whose traffic would leave the host directly:
that traffic shows its IP address. It is a reason not to run the service *here*, so
a peer can still run it, and the cost quote, the virtualizer and ResolveNetwork refuse
it for this node only.
"""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.gateway.launcher import launch_service as launch_mod
    from src.manager import network_defaults, networks, pow_networks
    from src.utils import network_policy
    from src.utils.config import ConfigManager
    from src.utils.network_policy import (
        HIDDEN_RULE,
        HiddenModeRejection,
        NetworkPolicyRejection,
        direct_egress_reason,
        enforce_hidden_mode,
        enforce_hidden_resolution,
        hidden_mode_rejection,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def override(**values):
    wanted = {key.replace("__", "."): value for key, value in values.items()}
    original = ConfigManager.get

    def get(self, key, default=None):
        return wanted[key] if key in wanted else original(self, key, default)

    return mock.patch.object(ConfigManager, "get", get)


def net(*tags):
    return celaut.Service.Network(tags=list(tags))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DirectEgressTests(unittest.TestCase):
    def test_the_constants_agree_with_the_resolvers(self):
        self.assertEqual(network_policy.POW_TAG_PREFIX, pow_networks.POW_TAG_PREFIX)
        self.assertEqual(network_policy.SEEDS_KEY, network_defaults.DEFAULT_INSTANCES_KEY)

    def test_what_leaves_the_host(self):
        seeds = {"redis": ["10.1.2.3:6379"], "empty": []}
        with override(service_networks__default_instances=seeds):
            self.assertIn("all egress", direct_egress_reason("*"))
            self.assertIn("proof-of-work", direct_egress_reason("pow:ergo"))
            self.assertIn("host name", direct_egress_reason("api.example.com"))
            self.assertIn("seeds", direct_egress_reason("redis"))
            # Only the local members answer these.
            self.assertIsNone(direct_egress_reason("postgres"))
            self.assertIsNone(direct_egress_reason("empty"))
            self.assertIsNone(direct_egress_reason("bitcoin"))
            self.assertIsNone(direct_egress_reason("Api.Example.com"))  # not a host-name tag
            self.assertIsNone(direct_egress_reason("  "))

    def test_a_node_that_is_not_hidden_refuses_nothing_here(self):
        with override(routing__HIDDEN=False):
            self.assertIsNone(hidden_mode_rejection([net("*"), net("pow:ergo")]))
            enforce_hidden_mode([net("*")])
            enforce_hidden_resolution(net("pow:ergo"))

    def test_a_hidden_node_allows_local_members_only(self):
        with override(routing__HIDDEN=True, service_networks__default_instances={}):
            self.assertIsNone(hidden_mode_rejection([net("postgres"), net("cache", "redis")]))
            self.assertIsNone(hidden_mode_rejection([]))

    def test_one_tag_that_leaves_the_host_refuses_the_entry(self):
        # Tags of an entry are synonyms and the resolver takes the first that answers.
        with override(routing__HIDDEN=True, service_networks__default_instances={}):
            rejection = hidden_mode_rejection(
                [net("postgres"), net("db", "db.example.com")], subject="service s"
            )
        self.assertEqual((rejection.tag, rejection.network_index), ("db.example.com", 2))
        self.assertEqual(rejection.rule, HIDDEN_RULE)
        report = rejection.report()
        self.assertIn("Hidden mode", report)
        self.assertIn("db.example.com", report)
        self.assertIn("host name", report)
        self.assertIn("service s", report)

    def test_the_raise_is_a_policy_rejection(self):
        # ResolveNetwork and the launcher already turn a NetworkPolicyRejection into
        # "this node does not reach that network".
        with override(routing__HIDDEN=True):
            with self.assertRaises(HiddenModeRejection) as caught:
                enforce_hidden_mode([net("*")])
        self.assertIsInstance(caught.exception, NetworkPolicyRejection)

    def test_resolve_network_refuses_only_pow_in_hidden_mode(self):
        with override(routing__HIDDEN=True):
            with self.assertRaises(HiddenModeRejection):
                enforce_hidden_resolution(net("pow:ergo"))
            # A host name or seeds are read from the resolver and the config: nobody
            # is contacted, so the answer shows nothing.
            enforce_hidden_resolution(net("api.example.com"))
            enforce_hidden_resolution(net("postgres"))

    def test_resolve_network_for_peer_stops_before_any_candidate_is_checked(self):
        with override(routing__HIDDEN=True), \
                mock.patch.object(networks, "resolve_network") as resolve:
            with self.assertRaises(HiddenModeRejection):
                networks.resolve_network_for_peer(net("pow:ergo"), subject="peer p")
        resolve.assert_not_called()

    def test_the_virtualizer_checks_again(self):
        from src.virtualizers.microvm import rootfs
        service = celaut.Service(network=[net("*")])
        with override(routing__HIDDEN=True), \
                mock.patch.object(rootfs, "enforce_network_policy"):
            with self.assertRaises(HiddenModeRejection):
                rootfs.build_network_resolution(service, father_id="")

    def test_the_quote_refuses_what_this_node_would_not_run(self):
        from src.gateway.iterables import estimated_cost_iterable as quote_mod
        iterable = quote_mod.GetServiceEstimatedCostIterable.__new__(
            quote_mod.GetServiceEstimatedCostIterable
        )
        iterable.service_hash = "svc"
        service = celaut.Service(network=[net("pow:bitcoin")])
        with override(routing__HIDDEN=True), \
                mock.patch.object(quote_mod, "read_service_from_disk", return_value=service), \
                mock.patch.object(quote_mod, "enforce_network_policy"):
            with self.assertRaises(HiddenModeRejection):
                iterable._quote()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchTests(unittest.TestCase):
    def _launch(self, service, candidates, hidden=True):
        cost = SimpleNamespace(cost=SimpleNamespace())
        mod = launch_mod
        with ExitStack() as stack:
            def patched(target, name, **kwargs):
                return stack.enter_context(mock.patch.object(target, name, **kwargs))

            stack.enter_context(override(routing__HIDDEN=hidden,
                                         service_networks__default_instances={}))
            patched(mod, "declarations_for_service", return_value=[])
            patched(mod, "service_requires_parent_colocation", return_value=False)
            patched(mod, "authorize_shares", return_value=[])
            spend = patched(mod, "spend_mu", return_value=True)
            balancer = patched(mod, "execution_balancer")
            local = patched(mod, "local_execution", return_value=SimpleNamespace(instance=None))
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
                    service=service, metadata=celaut.Metadata(),
                    configuration=celaut.Configuration(), father_id="client-1",
                    father_ip="", service_id="svc",
                )
            except Exception as e:
                error = e
            return SimpleNamespace(error=error, spend=spend, local=local, delegate=delegate,
                                   balancer=balancer)

    def test_a_peer_runs_what_the_hidden_node_will_not(self):
        r = self._launch(celaut.Service(network=[net("*")]), ["local", "peer-a"])
        self.assertIsNone(r.error)
        r.local.assert_not_called()
        r.spend.assert_called_once()
        self.assertEqual(r.delegate.call_args.kwargs["peer"], "peer-a")

    def test_with_no_peer_the_launch_fails_with_the_reason_and_no_charge(self):
        r = self._launch(celaut.Service(network=[net("pow:ergo")]), ["local"])
        self.assertIsNotNone(r.error)
        self.assertIn("hidden mode does not run 'pow:ergo'", str(r.error))
        r.spend.assert_not_called()
        r.local.assert_not_called()

    def test_local_members_only_still_run_here(self):
        r = self._launch(celaut.Service(network=[net("postgres")]), ["local"])
        self.assertIsNone(r.error)
        r.local.assert_called_once()

    def test_a_node_that_is_not_hidden_runs_it_here(self):
        r = self._launch(celaut.Service(network=[net("*")]), ["local"], hidden=False)
        self.assertIsNone(r.error)
        r.local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
