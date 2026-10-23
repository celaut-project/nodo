"""A node that delegates says so in its quote (#551).

What the tests protect:

* the commission applies to what the client consumes (the start charge of the peer,
  each tick), never to the deposit, and the minimum applies once, at the start;
* the quote is the best of the local price and a peer's price plus commission;
* each hop gives its peers less time than it has, and a node with too little time,
  or one already asking about the same service, answers with its local price only;
* the lists of the operator still end the quote: they refuse the domain everywhere;
* the launch charges the commission to the client, deposits only the peer's price on
  the peer, and does not delegate for more than it offered.
"""
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.balancers.execution_balancer import execution_balancer as balancer_mod
    from src.gateway.iterables import estimated_cost_iterable as cost_mod
    from src.gateway.launcher import launch_service as launch_mod
    from src.utils.cost_functions import delegated_quote, delegation_commission
    from src.utils.network_policy import HiddenModeRejection, NetworkPolicyRejection, Rejection
    from src.utils.singleton import Singleton
    from src.utils.tools.query_cache import QueryCache
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def amount(value):
    return celaut.Amount(n=str(value))


def price(cost, init=None, maximum=None):
    estimated = celaut.EstimatedCost(cost=amount(cost))
    if init is not None:
        estimated.init_maintenance_cost.CopyFrom(amount(init))
    if maximum is not None:
        estimated.max_maintenance_cost.CopyFrom(amount(maximum))
    return estimated


def cheapest_first(estimated_costs):
    """A stand-in for the sorter: by start cost only, no reputation."""
    return iter(sorted(estimated_costs.items(), key=lambda item: int(item[1].cost.n)))


class _FreshCache:
    def setUp(self):
        previous = Singleton._instances.pop(QueryCache, None)

        def restore():
            Singleton._instances.pop(QueryCache, None)
            if previous is not None:
                Singleton._instances[QueryCache] = previous

        self.addCleanup(restore)


def commission(percent=5.0, minimum=0):
    stack = ExitStack()
    stack.enter_context(mock.patch.object(delegation_commission, "commission_percent", return_value=percent))
    stack.enter_context(mock.patch.object(delegation_commission, "commission_min_mu", return_value=minimum))
    return stack


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CommissionTests(unittest.TestCase):
    def test_only_the_start_charge_pays_it_not_the_deposit(self):
        # 1 000 deposit + 200 start charge: 5 % of 200 is 10.
        with commission(5.0):
            offer = delegation_commission.with_commission(price(1_200), initial_mu=1_000)
        self.assertEqual(offer.cost.n, "1210")

    def test_the_maintenance_pays_it_too(self):
        with commission(5.0):
            offer = delegation_commission.with_commission(price(1_000, 100, 400), initial_mu=1_000)
        self.assertEqual((offer.cost.n, offer.init_maintenance_cost.n, offer.max_maintenance_cost.n),
                         ("1000", "105", "420"))

    def test_the_minimum_applies_to_the_start_only(self):
        with commission(5.0, minimum=50):
            offer = delegation_commission.with_commission(price(1_200, 100), initial_mu=1_000)
            self.assertEqual(delegation_commission.commission_on(100), 5)
        self.assertEqual(offer.cost.n, "1250")
        self.assertEqual(offer.init_maintenance_cost.n, "105")

    def test_it_rounds_up_and_stays_exact_on_large_amounts(self):
        with commission(5.0):
            self.assertEqual(delegation_commission.commission_on(1), 1)
            self.assertEqual(delegation_commission.commission_on(10**30 + 1), 5 * 10**28 + 1)

    def test_zero_percent_is_the_old_price(self):
        with commission(0.0):
            offer = delegation_commission.with_commission(price(1_200, 100), initial_mu=1_000)
        self.assertEqual((offer.cost.n, offer.init_maintenance_cost.n), ("1200", "100"))

    def test_a_bad_setting_reads_as_the_default(self):
        with mock.patch.object(delegation_commission.env_manager, "get", return_value="many"):
            self.assertEqual(delegation_commission.commission_percent(), delegation_commission.DEFAULT_PERCENT)
            self.assertEqual(delegation_commission.commission_min_mu(), delegation_commission.DEFAULT_MIN_MU)
        with mock.patch.object(delegation_commission.env_manager, "get", return_value=-3):
            self.assertEqual(delegation_commission.commission_percent(), delegation_commission.DEFAULT_PERCENT)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CommissionConfigTests(unittest.TestCase):
    def test_a_bad_commission_stops_the_node(self):
        from src.utils.config_validation import ConfigValidationError, validate_pricing_config

        for good in ({}, {"COMMISSION_PERCENT": 0}, {"COMMISSION_PERCENT": 2.5, "COMMISSION_MIN_MU": 10}):
            with self.subTest(value=good):
                validate_pricing_config({"network": {"delegation": good}})
        for bad in ({"COMMISSION_PERCENT": -1}, {"COMMISSION_PERCENT": "5%"}, {"COMMISSION_PERCENT": True},
                    {"COMMISSION_PERCENT": 5000}, {"COMMISSION_MIN_MU": 1.5}, {"COMMISSION_MIN_MU": -1}):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigValidationError):
                    validate_pricing_config({"network": {"delegation": bad}})
        with self.assertRaises(ConfigValidationError):
            validate_pricing_config({"network": {"delegation": [5]}})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TimeBudgetTests(unittest.TestCase):
    def test_each_hop_keeps_a_fifth_and_gives_the_rest(self):
        context = SimpleNamespace(time_remaining=lambda: 10.0)
        self.assertAlmostEqual(delegated_quote.delegation_time_budget(context), 8.0)

    def test_a_short_deadline_keeps_at_least_the_minimum_for_this_node(self):
        context = SimpleNamespace(time_remaining=lambda: 0.9)
        self.assertAlmostEqual(delegated_quote.delegation_time_budget(context), 0.7)

    def test_with_too_little_time_the_peers_are_not_asked(self):
        context = SimpleNamespace(time_remaining=lambda: 0.6)
        self.assertIsNone(delegated_quote.delegation_time_budget(context))

    def test_the_budget_ends_the_tree_of_questions(self):
        # Each hop passes on what it gives its peers. The chain stops by itself.
        remaining, hops = 10.0, 0
        while True:
            budget = delegated_quote.delegation_time_budget(SimpleNamespace(time_remaining=lambda: remaining))
            if budget is None:
                break
            remaining, hops = budget, hops + 1
        self.assertLess(hops, 20)

    def test_a_caller_with_no_deadline_gets_the_cost_timeout(self):
        context = SimpleNamespace(time_remaining=lambda: None)
        with mock.patch.object(delegated_quote.env_manager, "get", return_value=5):
            self.assertAlmostEqual(delegated_quote.delegation_time_budget(context), 4.0)
        with mock.patch.object(delegated_quote.env_manager, "get", return_value=5):
            self.assertAlmostEqual(delegated_quote.delegation_time_budget(None), 4.0)

    def test_the_no_deadline_of_grpc_gets_the_cost_timeout(self):
        # What grpcio gives a call with no deadline: not None (#556).
        context = SimpleNamespace(time_remaining=lambda: 9.223372036854776e18)
        with mock.patch.object(delegated_quote.env_manager, "get", return_value=5):
            self.assertAlmostEqual(delegated_quote.delegation_time_budget(context), 4.0)

    def test_the_budget_is_never_too_long_for_wait(self):
        for configured in (9.2e18, float("inf"), float("nan"), "x"):
            with mock.patch.object(delegated_quote.env_manager, "get", return_value=configured):
                budget = delegated_quote.delegation_time_budget(None)
            self.assertAlmostEqual(budget, 8.0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OneRoundPerServiceTests(_FreshCache, unittest.TestCase):
    def test_a_question_that_comes_back_does_not_start_a_second_round(self):
        with delegated_quote.asking_peers_for("svc") as first:
            self.assertTrue(first)
            with delegated_quote.asking_peers_for("svc") as again:
                self.assertFalse(again)
            with delegated_quote.asking_peers_for("other") as other:
                self.assertTrue(other)
        with delegated_quote.asking_peers_for("svc") as later:
            self.assertTrue(later)

    def test_the_round_ends_also_on_an_error(self):
        with self.assertRaises(RuntimeError):
            with delegated_quote.asking_peers_for("svc"):
                raise RuntimeError("peer exploded")
        with delegated_quote.asking_peers_for("svc") as first:
            self.assertTrue(first)

    def test_the_loop_key_does_not_see_the_deposit(self):
        # A -> B -> A converts the deposit twice, with a rounding: 100 comes back as 99.
        # A client that gives no deposit gets one from this node before the peers.
        keys = {
            delegated_quote.loop_key("svc", celaut.Configuration(initial_mu=amount(mu)))
            for mu in (100, 99)
        } | {delegated_quote.loop_key("svc", celaut.Configuration())}
        self.assertEqual(len(keys), 1)

    def test_the_loop_key_sees_the_rest_of_the_question(self):
        base = delegated_quote.loop_key("svc", celaut.Configuration())
        env = celaut.Configuration()
        env.environment_variables.add(key="MODE", value=b"fast")
        self.assertNotEqual(delegated_quote.loop_key("svc", env), base)
        self.assertNotEqual(delegated_quote.loop_key("other", celaut.Configuration()), base)

    def test_the_offer_is_kept_for_the_launch(self):
        self.assertIsNone(delegated_quote.recall_offer("client-1", "svc"))
        delegated_quote.remember_offer("client-1", "svc", 1_210)
        self.assertEqual(delegated_quote.recall_offer("client-1", "svc"), 1_210)
        self.assertIsNone(delegated_quote.recall_offer("client-2", "svc"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class QuotesFromPeersTests(unittest.TestCase):
    def _ask(self, delays, timeout):
        seen = {}

        def estimate(peer_id, timeout=None, **_):
            seen[peer_id] = timeout
            time.sleep(delays[peer_id])
            return price(100)

        with mock.patch.object(balancer_mod, "peers_id_iterator", return_value=iter(list(delays))), \
                mock.patch.object(balancer_mod, "should_skip_peer", return_value=False), \
                mock.patch.object(balancer_mod, "estimate_cost_on_peer", side_effect=estimate):
            started = time.monotonic()
            quotes = balancer_mod.quotes_from_peers(
                resources=celaut.Service.Container.Resources(), metadata=celaut.Metadata(),
                configuration=celaut.Configuration(), timeout=timeout,
            )
            return quotes, seen, time.monotonic() - started

    def test_the_peers_are_asked_at_the_same_time(self):
        quotes, _, elapsed = self._ask({"a": 0.3, "b": 0.3, "c": 0.3}, timeout=None)
        self.assertEqual(set(quotes), {"a", "b", "c"})
        self.assertLess(elapsed, 0.8)

    def test_a_late_peer_does_not_hold_the_answer_and_each_peer_gets_the_deadline(self):
        quotes, seen, elapsed = self._ask({"fast": 0.0, "late": 2.0}, timeout=0.4)
        self.assertEqual(set(quotes), {"fast"})
        self.assertLess(elapsed, 1.5)
        self.assertEqual(seen, {"fast": 0.4, "late": 0.4})

    def test_the_peer_call_gets_the_shorter_timeout(self):
        self.assertEqual(balancer_mod._shorter(10, 0.4), 0.4)
        self.assertEqual(balancer_mod._shorter(10, None), 10)
        self.assertEqual(balancer_mod._shorter(None, 0.4), 0.4)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RealGrpcDeadlineTests(unittest.TestCase):
    """The budget and the wait for the peers, on the context of a real grpc call (#556)."""

    def setUp(self):
        try:
            import grpc
        except ImportError as exc:  # pragma: no cover - environment-dependent
            self.skipTest(f"needs grpc: {exc}")
        from concurrent import futures

        def ask_the_peers(request, context):
            budget = delegated_quote.delegation_time_budget(context)
            quotes = balancer_mod.quotes_from_peers(
                resources=celaut.Service.Container.Resources(), metadata=celaut.Metadata(),
                configuration=celaut.Configuration(), timeout=budget,
            )
            return f"{budget!r} {sorted(quotes)}".encode()

        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(
            "test.Quote", {"Ask": grpc.unary_unary_rpc_method_handler(ask_the_peers)}),))
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.ask = self.channel.unary_unary("/test.Quote/Ask")
        self.addCleanup(self.server.stop, None)
        self.addCleanup(self.channel.close)

        settings = {"EXTERNAL_COST_TIMEOUT": 5}
        for patcher in (
            mock.patch.object(delegated_quote.env_manager, "get",
                              side_effect=lambda k, d=None: settings.get(k, d)),
            mock.patch.object(balancer_mod, "peers_id_iterator", side_effect=lambda **_: iter(["peer-a"])),
            mock.patch.object(balancer_mod, "should_skip_peer", return_value=False),
            # A peer that takes a moment, so that wait() waits with the budget.
            mock.patch.object(balancer_mod, "estimate_cost_on_peer",
                              side_effect=lambda **_: time.sleep(0.05) or price(1_200)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_call_with_no_deadline_gets_the_cost_timeout(self):
        self.assertEqual(self.ask(b""), b"4.0 ['peer-a']")

    def test_a_call_with_a_deadline_gets_a_part_of_it(self):
        budget, peers = self.ask(b"", timeout=10).decode().split(" ", 1)
        # The grpc-timeout header rounds up a little: about 8 s.
        self.assertTrue(7.0 < float(budget) < 8.5, budget)
        self.assertEqual(peers, "['peer-a']")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BalancerSortTests(unittest.TestCase):
    def test_a_peer_is_compared_with_its_commission_and_yielded_without_it(self):
        # The peer is cheaper alone (1 150 < 1 160), not with 5 % of its start charge.
        configuration = celaut.Configuration(initial_mu=amount(1_000))
        with commission(10.0), \
                mock.patch.object(balancer_mod, "estimated_cost_sorter", side_effect=cheapest_first):
            ordered = balancer_mod._sorted_with_commission(
                {"local": price(1_160), "peer-a": price(1_150)}, configuration)
        self.assertEqual([peer for peer, _ in ordered], ["local", "peer-a"])
        # 1 150 + 10 % of 150 = 1 165 is what decided, but the launcher gets 1 150.
        self.assertEqual(dict(ordered)["peer-a"].cost.n, "1150")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class QuoteTests(_FreshCache, unittest.TestCase):
    def _quote(self, peers, *, executes=True, local=None, budget=8.0, delegates=True,
               hidden_rejection=False, policy_rejection=False, client_id="client-1",
               deposit=1_000, metadata=None):
        it = cost_mod.GetServiceEstimatedCostIterable.__new__(cost_mod.GetServiceEstimatedCostIterable)
        it.configuration = celaut.Configuration(initial_mu=amount(deposit))
        it.service_hash = "svc"
        it.metadata = metadata or celaut.Metadata()
        it.client_id = client_id
        it.context = None
        rejection = Rejection(tag="*", network_index=1, rule="routing.HIDDEN", pattern=None,
                              declared=(("*",),), candidates=(), subject="svc",
                              reason="it opens all egress to the Internet")
        with ExitStack() as stack:
            def patched(target, name, **kwargs):
                return stack.enter_context(mock.patch.object(target, name, **kwargs))

            stack.enter_context(commission(5.0))
            settings = {"network.DELEGATE_EXECUTION": delegates}
            patched(cost_mod.env_manager, "get", side_effect=lambda k, d=None: settings.get(k, d))
            patched(cost_mod, "executes_locally", return_value=executes)
            patched(cost_mod, "read_service_from_disk", return_value=celaut.Service())
            patched(cost_mod, "enforce_network_policy",
                    side_effect=NetworkPolicyRejection(rejection) if policy_rejection else None)
            patched(cost_mod, "enforce_hidden_mode",
                    side_effect=HiddenModeRejection(rejection) if hidden_rejection else None)
            patched(cost_mod, "get_arch_tag", return_value="amd64")
            patched(cost_mod, "default_initial_balance", return_value=1_000)
            patched(cost_mod, "generate_estimated_cost", return_value=local or price(1_300))
            patched(cost_mod, "get_resource_availability", return_value={"can_execute": True})
            patched(cost_mod.activity_window, "is_open", return_value=True)
            patched(cost_mod.BeeClient, "respond",
                    side_effect=lambda message_iterator, indices: iter([message_iterator]))
            patched(cost_mod, "delegation_time_budget", return_value=budget)
            patched(cost_mod, "estimated_cost_sorter", side_effect=cheapest_first)
            asked = patched(cost_mod, "quotes_from_peers", return_value=peers)
            out = list(it.generate())
        return out[0], asked

    def test_a_node_that_does_not_execute_offers_a_peer_with_its_commission(self):
        offer, asked = self._quote({"peer-a": price(1_200)}, executes=False)
        self.assertEqual(offer.cost.n, "1210")
        asked.assert_called_once()
        self.assertEqual(asked.call_args.kwargs["timeout"], 8.0)
        # The deposit travels to the peer: the commission is not taken on it.
        self.assertEqual(asked.call_args.kwargs["configuration"].initial_mu.n, "1000")

    def test_a_peer_with_its_commission_is_offered_when_it_is_cheaper(self):
        offer, _ = self._quote({"peer-a": price(1_200)}, local=price(1_300))
        self.assertEqual(offer.cost.n, "1210")

    def test_the_local_price_is_offered_when_it_is_cheaper(self):
        # The peer alone (1 200) is cheaper, but not with its commission (1 210).
        offer, _ = self._quote({"peer-a": price(1_200)}, local=price(1_205))
        self.assertEqual(offer.cost.n, "1205")

    def test_with_no_peer_answer_the_local_price_is_offered_as_before(self):
        offer, _ = self._quote({}, local=price(1_300))
        self.assertEqual(offer.cost.n, "1300")

    def test_a_hidden_node_offers_what_a_peer_runs(self):
        offer, _ = self._quote({"peer-a": price(1_200)}, hidden_rejection=True)
        self.assertEqual(offer.cost.n, "1210")

    def test_with_no_peer_the_reason_of_this_node_is_given(self):
        with self.assertRaises(HiddenModeRejection):
            self._quote({}, hidden_rejection=True)

    def test_the_lists_of_the_operator_end_the_quote_without_asking_peers(self):
        with self.assertRaises(NetworkPolicyRejection):
            self._quote({"peer-a": price(1_200)}, policy_rejection=True)

    def test_without_time_the_peers_are_not_asked(self):
        offer, asked = self._quote({"peer-a": price(1)}, budget=None)
        self.assertEqual(offer.cost.n, "1300")
        asked.assert_not_called()

    def test_without_delegation_the_peers_are_not_asked(self):
        offer, asked = self._quote({"peer-a": price(1)}, delegates=False)
        self.assertEqual(offer.cost.n, "1300")
        asked.assert_not_called()

    def test_a_question_that_comes_back_through_a_loop_gets_the_local_price(self):
        # The round that is open had the deposit in other MU: 999, not 1 000.
        # The client sent its own metadata, and the question that comes back has the
        # metadata of the registry: the round stops on this node all the same.
        open_round = delegated_quote.loop_key(
            "svc", celaut.Configuration(initial_mu=amount(999)))
        client_metadata = celaut.Metadata()
        client_metadata.hashtag.hash.add(type=b"\x01", value=b"from-the-client")
        with delegated_quote.asking_peers_for(open_round):
            offer, asked = self._quote({"peer-a": price(1)}, metadata=client_metadata)
        self.assertEqual(offer.cost.n, "1300")
        asked.assert_not_called()

    def test_the_round_is_remembered_without_the_metadata(self):
        # The peers get the hashes only, and read the metadata from their registry.
        self._quote({"peer-a": price(1_200)})
        other = celaut.Metadata()
        other.hashtag.hash.add(type=b"\x01", value=b"from-the-client")
        _, asked = self._quote({"peer-a": price(1)}, metadata=other)
        asked.assert_not_called()

    def test_a_round_on_another_question_does_not_stop_this_one(self):
        env = celaut.Configuration()
        env.environment_variables.add(key="MODE", value=b"fast")
        with delegated_quote.asking_peers_for(delegated_quote.loop_key("svc", env)):
            offer, asked = self._quote({"peer-a": price(1_200)})
        self.assertEqual(offer.cost.n, "1210")
        asked.assert_called_once()

    def test_the_same_question_again_does_not_ask_the_peers_again(self):
        first, asked_first = self._quote({"peer-a": price(1_200)})
        second, asked_second = self._quote({"peer-a": price(1)})
        asked_first.assert_called_once()
        asked_second.assert_not_called()
        self.assertEqual((first.cost.n, second.cost.n), ("1210", "1210"))

    def test_the_round_is_remembered_by_the_full_question(self):
        # Another deposit is another price on the peer: the round is not reused.
        self._quote({"peer-a": price(1_200)})
        _, asked = self._quote({"peer-a": price(1_300)}, deposit=1_100)
        asked.assert_called_once()

    def test_an_empty_round_is_not_remembered(self):
        self._quote({})
        offer, asked = self._quote({"peer-a": price(1_200)})
        asked.assert_called_once()
        self.assertEqual(offer.cost.n, "1210")

    def test_with_no_peer_ttl_the_round_is_not_remembered(self):
        with mock.patch.object(delegated_quote, "peer_quote_ttl", return_value=0.0):
            self._quote({"peer-a": price(1_200)})
            _, asked = self._quote({"peer-a": price(1_200)})
        asked.assert_called_once()

    def test_the_offer_is_kept_for_the_launch(self):
        self._quote({"peer-a": price(1_200)}, executes=False)
        self.assertEqual(delegated_quote.recall_offer("client-1", "svc"), 1_210)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchTests(_FreshCache, unittest.TestCase):
    def _launch(self, candidates, offered=None, initial_mu=None, build_mu=0, minimum_mu=0):
        """Launch with ``candidates``. ``initial_mu`` None is a request with no amount.

        The default balance that the quotes ask for is 1 000 MU. ``build_mu`` and
        ``minimum_mu`` are what the local node would take for the build and for one
        maintenance period.
        """
        mod = launch_mod
        if offered is not None:
            delegated_quote.remember_offer("client-1", "svc", offered)
        configuration = celaut.Configuration()
        if initial_mu is not None:
            configuration.initial_mu.CopyFrom(amount(initial_mu))
        with ExitStack() as stack:
            def patched(target, name, **kwargs):
                return stack.enter_context(mock.patch.object(target, name, **kwargs))

            stack.enter_context(commission(5.0))
            patched(mod, "declarations_for_service", return_value=[])
            patched(mod, "service_requires_parent_colocation", return_value=False)
            patched(mod, "authorize_shares", return_value=[])
            spend = patched(mod, "spend_mu", return_value=True)
            balancer = patched(mod, "execution_balancer")
            local = patched(mod, "local_execution", return_value=SimpleNamespace(token="child", instance=None))
            delegate = patched(mod, "delegate_execution", return_value=SimpleNamespace(token="child", instance=None))
            patched(mod, "enforce_network_policy")
            patched(mod, "hidden_mode_rejection", return_value=None)
            patched(mod, "tor_egress_unready_reason", return_value=None)
            patched(mod, "evaluate_possible_environment_workloads", return_value=None)
            patched(mod, "_detect_local_preflight_failure", return_value=None)
            patched(mod, "descends_from_dev_client", return_value=True)
            patched(mod, "start_build_mu", return_value=build_mu)
            patched(mod, "minimum_runtime_mu", return_value=minimum_mu)
            patched(mod.activity_window, "is_open", return_value=True)
            patched(mod.demand_history, "record_admission")
            patched(mod.demand_history, "record_refusal")
            patched(mod.sc, "pop_forced_execution_peer", return_value=None)
            patched(mod.sc, "internal_instance_exists", return_value=False)
            patched(mod.utils, "get_network_name", return_value="lan")
            patched(mod, "get_arch_tag", return_value="amd64")
            patched(mod, "default_initial_balance", return_value=1_000)
            balancer.return_value = iter(candidates)
            error = None
            try:
                mod.launch_service(service=celaut.Service(), metadata=celaut.Metadata(),
                                   configuration=configuration,
                                   father_id="client-1", father_ip="", service_id="svc")
            except Exception as e:
                error = e
            return SimpleNamespace(error=error, spend=spend, delegate=delegate, local=local)

    def test_the_client_pays_its_initial_mu_and_the_commission_comes_out_of_it(self):
        # The quotes ask for a balance of 1 500, and the peer quotes it with a build of
        # 200: 1 700. The commission of this node on the build is 10.
        r = self._launch([("peer-a", price(1_700))], initial_mu=1_500)
        self.assertIsNone(r.error)
        self.assertEqual(r.spend.call_args.kwargs["amount_mu"], 1_500)
        kwargs = r.delegate.call_args.kwargs
        self.assertEqual(int(kwargs["config"].initial_mu.n), 1_500)
        self.assertEqual(kwargs["commission_mu"], 10)
        self.assertEqual(kwargs["build_mu"], 200)

    def test_with_no_amount_the_client_pays_the_quote_of_the_peer_and_its_commission(self):
        r = self._launch([("peer-a", price(1_200))])
        self.assertIsNone(r.error)
        self.assertEqual(r.spend.call_args.kwargs["amount_mu"], 1_210)
        kwargs = r.delegate.call_args.kwargs
        self.assertEqual(int(kwargs["config"].initial_mu.n), 1_210)
        # The peer gets 1 210 - 10 = 1 200: its build and the default balance.
        self.assertEqual(kwargs["commission_mu"], 10)
        self.assertEqual(kwargs["build_mu"], 200)

    def test_a_peer_that_costs_more_than_the_offer_is_not_used(self):
        r = self._launch([("peer-a", price(1_300)), ("peer-b", price(1_200))], offered=1_210)
        self.assertIsNone(r.error)
        self.assertEqual(r.delegate.call_args.kwargs["peer"], "peer-b")
        self.assertEqual(r.spend.call_count, 1)

    def test_with_no_peer_inside_the_offer_the_launch_fails_and_charges_nothing(self):
        r = self._launch([("peer-a", price(1_300))], offered=1_210)
        self.assertIsNotNone(r.error)
        self.assertIn("more than the 1210 MU this node offered", str(r.error))
        r.spend.assert_not_called()

    def test_a_client_that_sets_its_amount_is_never_charged_more_than_it_set(self):
        # The offer was 1 210, and the peer now costs more: a build of 300 on 1 500. The
        # client set 1 500: it is charged 1 500 and no more, and the start fits.
        r = self._launch([("peer-a", price(1_800))], offered=1_210, initial_mu=1_500)
        self.assertIsNone(r.error)
        self.assertEqual(r.spend.call_args.kwargs["amount_mu"], 1_500)

    def test_the_local_node_is_charged_its_quote_when_there_is_no_amount(self):
        r = self._launch([("local", price(1_300))], offered=1_210)
        self.assertIsNone(r.error)
        self.assertEqual(r.spend.call_args.kwargs["amount_mu"], 1_300)

    def test_the_local_node_is_charged_the_amount_that_the_client_set_and_no_build(self):
        r = self._launch([("local", price(1_300))], initial_mu=1_100, build_mu=300, minimum_mu=500)
        self.assertIsNone(r.error)
        self.assertEqual(r.spend.call_args.kwargs["amount_mu"], 1_100)
        self.assertEqual(int(r.local.call_args.kwargs["config"].initial_mu.n), 1_100)

    def test_an_amount_that_does_not_fit_the_build_charges_nothing(self):
        r = self._launch([("local", price(1_300))], initial_mu=200, build_mu=300)
        self.assertIsNotNone(r.error)
        self.assertIn("does not cover the build", str(r.error))
        r.spend.assert_not_called()
        r.local.assert_not_called()

    def test_an_amount_that_leaves_less_than_one_period_charges_nothing(self):
        r = self._launch([("local", price(1_300))], initial_mu=700, build_mu=300, minimum_mu=500)
        self.assertIsNotNone(r.error)
        self.assertIn("One maintenance period costs 500 MU", str(r.error))
        r.spend.assert_not_called()

    def test_a_peer_that_the_amount_does_not_fund_is_skipped_and_the_next_one_runs_it(self):
        # After the commission of 10 and the build of 200 of peer-a, 20 MU is left. One
        # period costs 50. The quote of peer-b has no build: 500 MU are left.
        cheap = price(430, init=50)
        free = price(230, init=50)
        r = self._launch([("peer-a", cheap), ("peer-b", free)], initial_mu=230)
        self.assertIsNone(r.error)
        self.assertEqual(r.delegate.call_args.kwargs["peer"], "peer-b")
        self.assertEqual(r.spend.call_count, 1)
        self.assertEqual(r.spend.call_args.kwargs["amount_mu"], 230)


if __name__ == "__main__":
    unittest.main()
