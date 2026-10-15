"""The recursion guard on StartService and the delegation it starts (issue #456).

Three layers, each on its own:

- ``RecursionGuard``/``Registry`` themselves: a missing token is a root, a held token
  is a loop, a token with no hops left is refused, a malformed one is refused, and
  what is forwarded is the same token with one hop less.
- A chain of nodes, simulated in one process by giving each "node" its own
  ``Registry`` (it is a per-process singleton): A -> B -> A is refused at A, and a chain
  longer than ``network.RECURSION_MAX_HOPS`` stops where the budget runs out.
- StartService (``launch_service``) accepts, validates and forwards the guard, and the
  balancer stops asking peers once the hops are spent.

The two query RPCs, GetServiceEstimatedCost and GetResourceAvailability, carry no guard:
they are protected by ``src/utils/tools/query_cache.py`` (see ``test_query_cache.py``).
"""
import contextlib
import threading
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.utils.singleton import Singleton
    from src.utils.tools import recursion_guard as rg
    from src.utils.tools.recursion_guard import (
        MalformedRecursionToken,
        RecursionDepthExhausted,
        RecursionGuard,
        RecursionLoop,
        RecursionRefused,
        Registry,
        recursion_guard_message,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

RUNTIME_IMPORT_ERROR = None
try:
    from src.balancers.execution_balancer import execution_balancer as balancer_mod
    from src.gateway.iterables import estimated_cost_iterable as cost_mod
    from src.gateway.launcher import launch_service as launch_service_mod
    from src.utils import utils as utils_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    RUNTIME_IMPORT_ERROR = import_exc

@contextlib.contextmanager
def _node():
    """Run the block as a separate node: a fresh, empty ``Registry`` of its own."""
    previous = Singleton._instances.pop(Registry, None)
    try:
        yield Registry()
    finally:
        if previous is None:
            Singleton._instances.pop(Registry, None)
        else:
            Singleton._instances[Registry] = previous


@contextlib.contextmanager
def _max_hops(value):
    with patch.object(rg, "max_hops", return_value=value):
        yield


def _hops(message):
    return message.remaining_hops if message.HasField("remaining_hops") else None


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuardTests(unittest.TestCase):

    def setUp(self):
        self._node = _node()
        self.registry = self._node.__enter__()
        self.addCleanup(self._node.__exit__, None, None, None)

    def test_a_missing_token_is_a_root_with_the_full_budget(self):
        with _max_hops(7), RecursionGuard(token=None, generate=True) as token:
            self.assertRegex(token, r"^[0-9a-f]{32}$")
            self.assertEqual(self.registry.tokens, {token: 7})
        self.assertEqual(self.registry.tokens, {})

    def test_an_empty_token_is_a_root_too(self):
        # protobuf cannot tell an unset string from an empty one, and neither can this.
        with RecursionGuard(token="", generate=True) as token:
            self.assertRegex(token, r"^[0-9a-f]{32}$")

    def test_a_callers_token_is_used_as_is_and_released(self):
        with RecursionGuard(token="tok-1", generate=True) as token:
            self.assertEqual(token, "tok-1")
            self.assertIn("tok-1", self.registry.tokens)
        self.assertNotIn("tok-1", self.registry.tokens)

    def test_a_token_already_held_is_a_loop(self):
        with RecursionGuard(token="tok-1", generate=True):
            with self.assertRaises(RecursionLoop) as ctx:
                RecursionGuard(token="tok-1", generate=True)
        # The message StartService has always refused a loop with.
        self.assertIn("Block recursion loop, recursion token: tok-1", str(ctx.exception))
        self.assertIsInstance(ctx.exception, RecursionRefused)
        # The refused arrival must not release the first holder's registration.
        self.assertEqual(self.registry.tokens, {})

    def test_a_loop_does_not_release_the_holder(self):
        with RecursionGuard(token="tok-1", generate=True):
            with self.assertRaises(RecursionLoop):
                RecursionGuard(token="tok-1", generate=True)
            self.assertIn("tok-1", self.registry.tokens)

    def test_no_hops_left_is_refused_and_nothing_is_registered(self):
        with self.assertRaises(RecursionDepthExhausted) as ctx:
            RecursionGuard(token="tok-1", generate=True, remaining_hops=0)
        self.assertIn("Recursion depth exhausted", str(ctx.exception))
        self.assertIsInstance(ctx.exception, RecursionRefused)
        self.assertEqual(self.registry.tokens, {})

    def test_a_senders_budget_is_clamped_to_this_nodes(self):
        with _max_hops(4), RecursionGuard(token="tok-1", generate=True, remaining_hops=1000):
            self.assertEqual(self.registry.tokens["tok-1"], 4)
        with _max_hops(4), RecursionGuard(token="tok-1", generate=True, remaining_hops=2):
            self.assertEqual(self.registry.tokens["tok-1"], 2)

    def test_malformed_tokens_are_refused(self):
        for bad in ("has space", "x" * 129, "tok\n", "tök", "a;b", "../etc"):
            with self.subTest(token=bad):
                with self.assertRaises(MalformedRecursionToken):
                    RecursionGuard(token=bad, generate=True)
        self.assertEqual(self.registry.tokens, {})

    def test_tokens_this_codebase_uses_are_well_formed(self):
        for good in ("0" * 32, "tok-1", "token-a", "a.b:c_d", "x" * 128):
            with self.subTest(token=good), RecursionGuard(token=good, generate=True):
                pass

    def test_generate_false_is_no_guard_at_all(self):
        # launch_service's path for a launch asked for by one of this node's own
        # instances: unchanged by #456, the caller's token is dropped.
        with RecursionGuard(token="tok-1", generate=False, remaining_hops=0) as token:
            self.assertIsNone(token)
            self.assertEqual(self.registry.tokens, {})

    def test_the_forwarded_guard_is_the_same_token_one_hop_less(self):
        with RecursionGuard(token="tok-1", generate=True, remaining_hops=5):
            message = recursion_guard_message("tok-1")
            self.assertEqual(message.token, "tok-1")
            self.assertEqual(_hops(message), 4)
            self.assertTrue(self.registry.can_forward("tok-1"))

    def test_the_last_hop_cannot_be_forwarded(self):
        with RecursionGuard(token="tok-1", generate=True, remaining_hops=1):
            self.assertFalse(self.registry.can_forward("tok-1"))
            self.assertEqual(_hops(recursion_guard_message("tok-1")), 0)

    def test_a_token_this_node_does_not_hold_is_forwarded_without_a_count(self):
        message = recursion_guard_message("tok-1")
        self.assertEqual(message.token, "tok-1")
        self.assertIsNone(_hops(message))
        self.assertTrue(self.registry.can_forward("tok-1"))
        self.assertIsNone(recursion_guard_message(None))
        self.assertTrue(self.registry.can_forward(None))

    def test_concurrent_arrivals_of_one_token_admit_exactly_one(self):
        barrier = threading.Barrier(8)
        outcomes = []

        def arrive():
            barrier.wait()
            try:
                self.registry.add("tok-race", 3)
                outcomes.append("held")
            except RecursionLoop:
                outcomes.append("loop")

        threads = [threading.Thread(target=arrive) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["held"] + ["loop"] * 7)

    def test_the_configured_maximum(self):
        with patch.object(rg, "max_hops", wraps=rg.max_hops):
            self.assertEqual(rg.max_hops(), 16)  # config.example.yaml
        with patch("src.utils.config.ConfigManager.get", return_value="nonsense"):
            self.assertEqual(rg.max_hops(), rg.DEFAULT_MAX_HOPS)
        with patch("src.utils.config.ConfigManager.get", return_value=0):
            self.assertEqual(rg.max_hops(), rg.DEFAULT_MAX_HOPS)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChainTests(unittest.TestCase):
    """Several nodes, each with its own registry, passing one request along."""

    @contextlib.contextmanager
    def _as(self, nodes, name):
        previous = Singleton._instances.get(Registry)
        Singleton._instances[Registry] = nodes[name]
        try:
            yield
        finally:
            if previous is None:
                Singleton._instances.pop(Registry, None)
            else:
                Singleton._instances[Registry] = previous

    def _nodes(self, *names):
        nodes = {}
        for name in names:
            with _node() as registry:
                nodes[name] = registry
        return nodes

    def _forward(self, nodes, sender, token):
        with self._as(nodes, sender):
            return recursion_guard_message(token)

    def _accept(self, nodes, receiver, message, stack):
        with self._as(nodes, receiver):
            guard = RecursionGuard(
                token=message.token if message is not None else None,
                generate=True,
                remaining_hops=_hops(message) if message is not None else None,
            )
        stack.callback(lambda: self._release(nodes, receiver, guard))
        return guard.token

    def _release(self, nodes, receiver, guard):
        with self._as(nodes, receiver):
            guard.__exit__(None, None, None)

    def test_a_to_b_to_a_is_refused_at_a(self):
        nodes = self._nodes("A", "B")
        with contextlib.ExitStack() as stack, _max_hops(16):
            token = self._accept(nodes, "A", None, stack)  # a root at A
            self._accept(nodes, "B", self._forward(nodes, "A", token), stack)
            with self.assertRaises(RecursionLoop):
                self._accept(nodes, "A", self._forward(nodes, "B", token), stack)
        for registry in nodes.values():
            self.assertEqual(registry.tokens, {})

    def test_a_to_b_to_c_to_a_is_refused_at_a(self):
        nodes = self._nodes("A", "B", "C")
        with contextlib.ExitStack() as stack, _max_hops(16):
            token = self._accept(nodes, "A", None, stack)
            self._accept(nodes, "B", self._forward(nodes, "A", token), stack)
            self._accept(nodes, "C", self._forward(nodes, "B", token), stack)
            with self.assertRaises(RecursionLoop):
                self._accept(nodes, "A", self._forward(nodes, "C", token), stack)

    def test_the_same_node_may_serve_the_same_tree_again_once_it_is_done(self):
        # Not a loop: sequential, not nested. A's balancer asks B for a quote and then
        # asks B to run it, both with the same token.
        nodes = self._nodes("A", "B")
        with contextlib.ExitStack() as stack, _max_hops(16):
            token = self._accept(nodes, "A", None, stack)
            with contextlib.ExitStack() as quote:
                self._accept(nodes, "B", self._forward(nodes, "A", token), quote)
            self._accept(nodes, "B", self._forward(nodes, "A", token), stack)

    def test_the_budget_counts_every_node_and_stops_the_chain(self):
        names = ["N0", "N1", "N2", "N3"]
        nodes = self._nodes(*names)
        with contextlib.ExitStack() as stack, _max_hops(3):
            token = self._accept(nodes, "N0", None, stack)
            self._accept(nodes, "N1", self._forward(nodes, "N0", token), stack)
            self._accept(nodes, "N2", self._forward(nodes, "N1", token), stack)
            # N2 holds the last hop: it may run the work, not pass it on.
            with self._as(nodes, "N2"):
                self.assertFalse(Registry().can_forward(token))
            # A node that passes it on anyway is refused downstream.
            with self.assertRaises(RecursionDepthExhausted):
                self._accept(nodes, "N3", self._forward(nodes, "N2", token), stack)

    def test_a_node_that_predates_the_field_resets_only_the_count(self):
        # B drops `remaining_hops` (a node from before #456 does exactly that): C
        # starts a fresh budget, but the token -- forwarded by every version -- still
        # catches the loop back to A.
        nodes = self._nodes("A", "B", "C")
        with contextlib.ExitStack() as stack, _max_hops(2):
            token = self._accept(nodes, "A", None, stack)
            self._accept(nodes, "B", self._forward(nodes, "A", token), stack)
            legacy = celaut.RecursionGuard(token=token)
            self._accept(nodes, "C", legacy, stack)
            with self._as(nodes, "C"):
                self.assertEqual(Registry().tokens[token], 2)
            with self.assertRaises(RecursionLoop):
                self._accept(nodes, "A", self._forward(nodes, "C", token), stack)


def _patch_policy():
    from src.utils import network_policy as np
    return patch.object(
        np.NetworkPolicy, "from_config",
        classmethod(lambda cls, env_manager=None: np.NetworkPolicy()),
    )


@unittest.skipIf(
    IMPORT_ERROR is not None or RUNTIME_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or RUNTIME_IMPORT_ERROR}",
)
class StartServiceTests(unittest.TestCase):
    """``launch_service`` is where StartService holds the guard; it must not change."""

    def setUp(self):
        self._node = _node()
        self.registry = self._node.__enter__()
        self.addCleanup(self._node.__exit__, None, None, None)

    def _launch(self, token, hops=None, father_id="dev-client-1", balancer=None):
        configuration = celaut.Configuration()
        configuration.initial_mu.n = "1000"
        seen = {}

        def _balancer(**kwargs):
            seen["token"] = kwargs["recursion_guard_token"]
            seen["registry"] = dict(Registry().tokens)
            seen["forwarded"] = recursion_guard_message(kwargs["recursion_guard_token"])
            return iter([])

        with _patch_policy(), patch.object(
            launch_service_mod.sc, "pop_forced_execution_peer", return_value=None
        ), patch.object(
            launch_service_mod.sc, "get_local_instance_id_by_uri", return_value="instance-1"
        ), patch.object(
            launch_service_mod, "_detect_local_preflight_failure", return_value=None
        ), patch.object(
            launch_service_mod, "execution_balancer", side_effect=balancer or _balancer
        ) as mocked:
            try:
                launch_service_mod.launch_service(
                    service=celaut.Service(),
                    metadata=celaut.Metadata(),
                    father_ip="10.0.0.1",
                    father_id=father_id,
                    service_id="svc-1",
                    configuration=configuration,
                    recursion_guard_token=token,
                    recursion_guard_hops=hops,
                )
            except RecursionRefused:
                raise
            except Exception:
                pass  # No candidate -- the balancer was stubbed to offer none.
        return seen, mocked

    def test_the_callers_token_is_held_and_handed_to_the_balancer(self):
        with _max_hops(16):
            seen, _ = self._launch("tok-1")
        self.assertEqual(seen["token"], "tok-1")
        self.assertEqual(seen["registry"], {"tok-1": 16})
        self.assertEqual(self.registry.tokens, {})

    def test_a_missing_token_is_a_root(self):
        seen, _ = self._launch(None)
        self.assertRegex(seen["token"], r"^[0-9a-f]{32}$")

    def test_a_loop_is_refused_before_the_balancer(self):
        self.registry.add("tok-1", 5)
        with self.assertRaises(RecursionLoop):
            _, mocked = self._launch("tok-1")
        self.assertEqual(self.registry.tokens, {"tok-1": 5})

    def test_no_hops_left_is_refused_before_the_balancer(self):
        with self.assertRaises(RecursionDepthExhausted):
            self._launch("tok-1", hops=0)
        self.assertEqual(self.registry.tokens, {})

    def test_malformed_token_is_refused(self):
        with self.assertRaises(MalformedRecursionToken):
            self._launch("bad token")

    def test_what_is_delegated_carries_the_token_one_hop_less(self):
        seen, _ = self._launch("tok-1", hops=6)
        self.assertEqual(seen["forwarded"].token, "tok-1")
        self.assertEqual(_hops(seen["forwarded"]), 5)

    def test_a_launch_from_a_local_instance_is_still_unguarded(self):
        # father_id None -> the requester is one of this node's instances, and the
        # guard stays off (generate=False), the caller's token dropped -- unchanged.
        seen, _ = self._launch("tok-1", father_id=None)
        self.assertIsNone(seen["token"])
        self.assertEqual(seen["registry"], {})

    def test_the_iterable_hands_the_received_hops_to_the_launcher(self):
        from src.gateway.iterables import start_service_iterable as ssi
        it = ssi.StartServiceIterable.__new__(ssi.StartServiceIterable)
        it.configuration = None
        it.service_hash = "abc"
        it.metadata = celaut.Metadata()
        it.client_id = "client-1"
        it.recursion_guard_token = "tok-1"
        it.recursion_guard_hops = 3
        it.context = type("Ctx", (), {
            "peer": lambda self: "ipv4:10.0.0.1:1",
            "is_active": lambda self: True,
        })()
        with patch.object(ssi, "read_service_from_disk", return_value=celaut.Service()), \
                patch.object(ssi, "get_service_hex_main_hash", return_value="abc"), \
                patch.object(ssi, "launch_service", return_value=iter([])) as launch, \
                patch.object(ssi.BeeClient, "respond", side_effect=lambda **kw: iter([])):
            list(it.generate())
        self.assertEqual(launch.call_args.kwargs["recursion_guard_token"], "tok-1")
        self.assertEqual(launch.call_args.kwargs["recursion_guard_hops"], 3)

    def test_the_balancer_asks_no_peer_once_the_hops_are_spent(self):
        with RecursionGuard(token="tok-1", generate=True, remaining_hops=1), \
                patch.object(balancer_mod, "generate_estimated_cost", return_value=celaut.EstimatedCost()), \
                patch.object(balancer_mod, "peers_id_iterator") as peers, \
                patch.object(balancer_mod, "estimated_cost_sorter", side_effect=lambda estimated_costs: list(estimated_costs)):
            candidates = balancer_mod.execution_balancer(
                service_id="svc-1",
                resources=celaut.Service.Container.Resources(),
                metadata=celaut.Metadata(),
                configuration=celaut.Configuration(),
                recursion_guard_token="tok-1",
            )
        peers.assert_not_called()
        self.assertEqual(candidates, ["local"])

    def test_the_balancer_still_asks_peers_while_hops_remain_and_sends_no_guard_on_the_quote(self):
        sent, asked = [], []

        def _cost(channel, message_iterator, timeout=None):
            asked.append("peer-a")
            sent.extend(m for m in message_iterator if isinstance(m, celaut.RecursionGuard))
            return celaut.EstimatedCost()

        with RecursionGuard(token="tok-1", generate=True, remaining_hops=4), \
                patch.object(balancer_mod, "generate_estimated_cost", return_value=None), \
                patch.object(balancer_mod, "peers_id_iterator", return_value=iter(["peer-a"])), \
                patch.object(balancer_mod, "should_skip_peer", return_value=False), \
                patch.object(balancer_mod, "peer_channel"), \
                patch.object(balancer_mod, "get_client_id_on_other_peer", return_value="c"), \
                patch.object(balancer_mod, "matching_payment_system", return_value=object()), \
                patch.object(balancer_mod, "configuration_for_peer", side_effect=lambda c, **_: c), \
                patch.object(balancer_mod, "estimated_cost_for_local", side_effect=lambda e, **_: e), \
                patch.object(balancer_mod.BeeClient, "get_service_estimated_cost", side_effect=_cost), \
                patch.object(balancer_mod, "estimated_cost_sorter", side_effect=lambda estimated_costs: list(estimated_costs)):
            balancer_mod.execution_balancer(
                service_id="svc-1",
                resources=celaut.Service.Container.Resources(),
                metadata=celaut.Metadata(hashtag=celaut.Metadata.HashTag()),
                configuration=celaut.Configuration(),
                recursion_guard_token="tok-1",
            )
        # A quote is answered from the peer's own machine and never passed on, so it
        # carries no guard; the hops matter again when a peer is selected to run it.
        self.assertEqual(asked, ["peer-a"])
        self.assertEqual(sent, [])


@unittest.skipIf(
    IMPORT_ERROR is not None or RUNTIME_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or RUNTIME_IMPORT_ERROR}",
)
class EnvelopeTests(unittest.TestCase):
    """The StartService envelope, which a delegated launch is sent in."""

    def test_the_envelope_carries_the_guard_at_index_2(self):
        from protos.gateway_bee import StartService_input_indices
        self.assertIs(StartService_input_indices[2], celaut.RecursionGuard)
        with RecursionGuard(token="tok-1", generate=True, remaining_hops=9):
            sent = list(utils_mod.service_extended(
                metadata=celaut.Metadata(), send_only_hashes=True,
                client_id="c", recursion_guard_token="tok-1",
            ))
        guards = [m for m in sent if isinstance(m, celaut.RecursionGuard)]
        self.assertEqual([(m.token, _hops(m)) for m in guards], [("tok-1", 8)])
        # Without a token nothing is sent, as before #456.
        sent = list(utils_mod.service_extended(
            metadata=celaut.Metadata(), send_only_hashes=True, client_id="c",
        ))
        self.assertFalse([m for m in sent if isinstance(m, celaut.RecursionGuard)])


if __name__ == "__main__":
    unittest.main()
