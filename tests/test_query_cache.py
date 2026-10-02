"""The query cache that replaces the recursion guard on the two query RPCs (issue #456).

- ``QueryCache`` on its own: unknown -> compute, in progress -> refuse, done -> serve
  until the TTL; an error is never remembered; the size is bounded; one question is
  computed once under concurrency.
- ``canonical_key``: field order, unknown fields and the order of metadata hashes cannot
  vary the key, while anything that changes the answer does.
- A chain of nodes, one cache per simulated node: A -> B -> A is refused at A.
- The RPCs: GetServiceEstimatedCost and GetResourceAvailability answer the same question
  from memory, and a closed node never serves a remembered answer. The asking side keeps
  the answers it already has.
"""
import contextlib
import threading
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.utils.singleton import Singleton
    from src.utils.tools import query_cache as qc
    from src.utils.tools.query_cache import (
        HIT,
        IN_PROGRESS,
        MISS,
        QueryCache,
        QueryInProgress,
        canonical_key,
        with_sorted_hashes,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

RUNTIME_IMPORT_ERROR = None
try:
    from src.balancers.execution_balancer import execution_balancer as balancer_mod
    from src.gateway.iterables import estimated_cost_iterable as cost_mod
    from src.gateway.iterables import resource_availability_iterable as avail_mod
    from src.utils.cost_functions import workload_admission as admission_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    RUNTIME_IMPORT_ERROR = import_exc


@contextlib.contextmanager
def _node():
    """Run the block as a separate node: a fresh, empty ``QueryCache`` of its own."""
    previous = Singleton._instances.pop(QueryCache, None)
    try:
        yield QueryCache()
    finally:
        if previous is None:
            Singleton._instances.pop(QueryCache, None)
        else:
            Singleton._instances[QueryCache] = previous


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CacheTests(unittest.TestCase):

    def setUp(self):
        self._node = _node()
        self.cache = self._node.__enter__()
        self.addCleanup(self._node.__exit__, None, None, None)

    def test_unknown_is_computed_once_and_then_served(self):
        compute = MagicMock(return_value="answer")
        self.assertEqual(self.cache.get_or_compute("k", 30, compute), "answer")
        self.assertEqual(self.cache.get_or_compute("k", 30, compute), "answer")
        compute.assert_called_once()

    def test_a_question_in_progress_is_refused_not_answered_or_awaited(self):
        def compute():
            with self.assertRaises(QueryInProgress):
                self.cache.get_or_compute("k", 30, lambda: "never")
            return "answer"

        self.assertEqual(self.cache.get_or_compute("k", 30, compute), "answer")
        self.assertEqual(self.cache.get_or_compute("k", 30, compute), "answer")

    def test_an_error_is_not_remembered(self):
        with self.assertRaises(ValueError):
            self.cache.get_or_compute("k", 30, MagicMock(side_effect=ValueError))
        self.assertEqual(self.cache.begin("k"), (MISS, None))

    def test_an_answer_expires_after_its_ttl(self):
        clock = _Clock()
        compute = MagicMock(side_effect=["old", "new"])
        with patch.object(qc.time, "monotonic", clock):
            self.assertEqual(self.cache.get_or_compute("k", 30, compute), "old")
            clock.now += 29
            self.assertEqual(self.cache.get_or_compute("k", 30, compute), "old")
            clock.now += 2
            self.assertEqual(self.cache.get_or_compute("k", 30, compute), "new")

    def test_a_ttl_of_zero_turns_the_cache_off(self):
        compute = MagicMock(side_effect=["a", "b"])
        self.assertEqual(self.cache.get_or_compute("k", 0, compute), "a")
        self.assertEqual(self.cache.get_or_compute("k", 0, compute), "b")

    def test_the_size_is_bounded_and_the_least_recently_used_goes_first(self):
        with patch.object(qc, "max_entries", return_value=3):
            for key in ("a", "b", "c"):
                self.cache.get_or_compute(key, 30, lambda key=key: key)
            self.cache.get_or_compute("a", 30, lambda: "stale")  # a is now the freshest
            self.cache.get_or_compute("d", 30, lambda: "d")
        self.assertEqual(self.cache.begin("b")[0], MISS)
        self.cache.abort("b")
        self.assertEqual(self.cache.begin("a"), (HIT, "a"))

    def test_a_pending_entry_is_never_evicted(self):
        with patch.object(qc, "max_entries", return_value=1):
            self.assertEqual(self.cache.begin("k")[0], MISS)
            for key in ("x", "y"):
                self.cache.get_or_compute(key, 30, lambda: 1)
        self.assertEqual(self.cache.begin("k")[0], IN_PROGRESS)

    def test_concurrent_arrivals_of_one_question_compute_it_once(self):
        gate = threading.Event()
        started = threading.Event()
        calls = []
        outcomes = []

        def compute():
            calls.append(1)
            started.set()
            gate.wait(5)
            return "answer"

        def ask():
            try:
                outcomes.append(self.cache.get_or_compute("k", 30, compute))
            except QueryInProgress:
                outcomes.append("retry")

        first = threading.Thread(target=ask)
        first.start()
        self.assertTrue(started.wait(5))
        others = [threading.Thread(target=ask) for _ in range(7)]
        for t in others:
            t.start()
        for t in others:
            t.join(5)
        gate.set()
        first.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(outcomes), ["answer"] + ["retry"] * 7)

    def test_asking_does_not_refuse_a_concurrent_identical_question(self):
        # `recall` and `remember` are what a node asking a peer uses: it never claims a key.
        self.assertEqual(self.cache.recall("k"), (MISS, None))
        self.assertEqual(self.cache.recall("k"), (MISS, None))
        self.cache.remember("k", "answer", 30)
        self.assertEqual(self.cache.recall("k"), (HIT, "answer"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class KeyTests(unittest.TestCase):

    def _resources(self, mem=100):
        r = celaut.ArchitectureResources()
        r.resources.mem_limit = mem
        return r

    def test_the_same_question_has_the_same_key(self):
        self.assertEqual(
            canonical_key("rpc", self._resources()), canonical_key("rpc", self._resources())
        )

    def test_a_changed_resource_is_another_question(self):
        self.assertNotEqual(
            canonical_key("rpc", self._resources(100)), canonical_key("rpc", self._resources(101))
        )

    def test_the_rpc_and_the_asked_peer_are_part_of_the_key(self):
        r = self._resources()
        self.assertNotEqual(canonical_key("a", r), canonical_key("b", r))
        self.assertNotEqual(canonical_key("rpc", "peer-1", r), canonical_key("rpc", "peer-2", r))

    def test_fields_this_node_does_not_know_do_not_vary_the_key(self):
        plain = self._resources()
        padded = celaut.ArchitectureResources.FromString(
            plain.SerializeToString() + b"\xa2\x06\x03abc"  # an unknown field 100, length-delimited
        )
        self.assertNotEqual(plain.SerializeToString(), padded.SerializeToString())
        self.assertEqual(canonical_key("rpc", plain), canonical_key("rpc", padded))

    def test_the_order_of_metadata_hashes_does_not_vary_the_key(self):
        def metadata(order):
            m = celaut.Metadata()
            for t, v in order:
                m.hashtag.hash.add(type=t, value=v)
            return m

        one = metadata([(b"\x01", b"a"), (b"\x02", b"b")])
        other = metadata([(b"\x02", b"b"), (b"\x01", b"a")])
        self.assertNotEqual(
            canonical_key("rpc", one), canonical_key("rpc", other)
        )
        self.assertEqual(
            canonical_key("rpc", with_sorted_hashes(one)),
            canonical_key("rpc", with_sorted_hashes(other)),
        )

    def test_the_input_is_not_modified(self):
        m = celaut.Metadata()
        m.hashtag.hash.add(type=b"\x02", value=b"b")
        m.hashtag.hash.add(type=b"\x01", value=b"a")
        with_sorted_hashes(m)
        self.assertEqual([h.type for h in m.hashtag.hash], [b"\x02", b"\x01"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChainTests(unittest.TestCase):
    """One cache per simulated node, the way the guard tests give each node a registry."""

    def _ask(self, nodes, name, question, route):
        """``name`` answers ``question``; to do so it asks the next node on ``route``."""
        with _use(nodes[name]):
            def compute():
                if not route:
                    return f"answered by {name}"
                return self._ask(nodes, route[0], question, route[1:])
            return QueryCache().get_or_compute(canonical_key("rpc", question), 30, compute)

    def test_a_to_b_to_a_is_refused_at_a(self):
        nodes = {"A": MagicMock(), "B": MagicMock()}
        with _node() as a, _node() as b:
            nodes["A"], nodes["B"] = a, b
            with self.assertRaises(QueryInProgress):
                self._ask(nodes, "A", "same question", ["B", "A"])
            # Nothing is left in progress at either node, and nothing was remembered.
            self.assertEqual(a.begin(canonical_key("rpc", "same question"))[0], MISS)
            self.assertEqual(b.begin(canonical_key("rpc", "same question"))[0], MISS)

    def test_a_chain_without_a_return_is_answered(self):
        with _node() as a, _node() as b, _node() as c:
            nodes = {"A": a, "B": b, "C": c}
            self.assertEqual(
                self._ask(nodes, "A", "q", ["B", "C"]), "answered by C"
            )

    def test_a_forwarder_that_changes_the_question_gets_it_computed_again(self):
        # No loop is detected, because the content differs: the price of each call is
        # what bounds that, not the cache (docs/proposals/456-recursion-guard-incentives.md).
        with _node() as a:
            calls = []

            def hop(question, depth):
                def compute():
                    calls.append(question)
                    return hop(question + "+", depth - 1) if depth else "done"
                return a.get_or_compute(canonical_key("rpc", question), 30, compute)

            self.assertEqual(hop("q", 3), "done")
            self.assertEqual(calls, ["q", "q+", "q++", "q+++"])


@contextlib.contextmanager
def _use(cache):
    previous = Singleton._instances.get(QueryCache)
    Singleton._instances[QueryCache] = cache
    try:
        yield
    finally:
        if previous is None:
            Singleton._instances.pop(QueryCache, None)
        else:
            Singleton._instances[QueryCache] = previous


@unittest.skipIf(
    IMPORT_ERROR is not None or RUNTIME_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or RUNTIME_IMPORT_ERROR}",
)
class EstimatedCostRpcTests(unittest.TestCase):

    def setUp(self):
        self._node = _node()
        self.cache = self._node.__enter__()
        self.addCleanup(self._node.__exit__, None, None, None)
        self.window_open = True

    def _quote(self, hash_order=("a", "b"), initial_mu=None, token=None):
        it = cost_mod.GetServiceEstimatedCostIterable.__new__(cost_mod.GetServiceEstimatedCostIterable)
        it.configuration = celaut.Configuration()
        it.service_hash = "abc123"
        it.metadata = celaut.Metadata()
        for h in hash_order:
            it.metadata.hashtag.hash.add(type=h.encode(), value=h.encode())
        it.recursion_guard_token = token
        it.recursion_guard_hops = None
        with patch.object(cost_mod, "read_service_from_disk", return_value=celaut.Service()), \
                patch.object(cost_mod, "enforce_network_policy"), \
                patch.object(cost_mod, "default_initial_balance", return_value=1), \
                patch.object(cost_mod, "generate_estimated_cost", side_effect=lambda **_: celaut.EstimatedCost()) as quote, \
                patch.object(cost_mod.BeeClient, "respond", side_effect=lambda message_iterator, indices: iter([message_iterator])), \
                patch.object(cost_mod.activity_window, "is_open", side_effect=lambda: self.window_open), \
                patch.object(cost_mod, "to_amount", side_effect=lambda v: celaut.Amount()):
            self._quote_mock = quote
            return list(it.generate())

    def test_the_same_quote_is_computed_once(self):
        with patch.object(qc, "quote_ttl", return_value=30):
            with patch.object(cost_mod, "generate_estimated_cost", wraps=None):
                pass
            calls = 0
            for order in (("a", "b"), ("b", "a")):
                self._quote(hash_order=order)
                calls += self._quote_mock.call_count
        self.assertEqual(calls, 1)

    def test_a_different_quote_is_computed_again(self):
        calls = 0
        for h in (("a",), ("a", "b")):
            self._quote(hash_order=h)
            calls += self._quote_mock.call_count
        self.assertEqual(calls, 2)

    def test_a_closed_node_never_serves_a_remembered_quote(self):
        self._quote()
        self.window_open = False
        with self.assertRaises(Exception) as ctx:
            self._quote()
        self.assertNotIsInstance(ctx.exception, QueryInProgress)
        self._quote_mock.assert_not_called()

    def test_a_token_on_the_quote_is_neither_held_nor_part_of_the_question(self):
        from src.utils.tools.recursion_guard import Registry
        calls = 0
        for token in (None, "tok-1", "tok-2"):
            self._quote(token=token)
            calls += self._quote_mock.call_count
            self.assertNotIn("tok-1", Registry().tokens)
        self.assertEqual(calls, 1)

    def test_a_quote_asked_while_it_is_computed_is_refused(self):
        refused = []
        it = cost_mod.GetServiceEstimatedCostIterable.__new__(cost_mod.GetServiceEstimatedCostIterable)
        it.configuration, it.service_hash, it.metadata = celaut.Configuration(), "abc123", celaut.Metadata()
        it.recursion_guard_token = it.recursion_guard_hops = None

        def slow_quote(**_):
            # The same question arrives while this one is still being computed.
            try:
                list(it.generate())
            except QueryInProgress:
                refused.append(True)
            return celaut.EstimatedCost()

        with patch.object(cost_mod, "generate_estimated_cost", side_effect=slow_quote), \
                patch.object(cost_mod, "read_service_from_disk", return_value=celaut.Service()), \
                patch.object(cost_mod, "enforce_network_policy"), \
                patch.object(cost_mod, "default_initial_balance", return_value=1), \
                patch.object(cost_mod, "to_amount", side_effect=lambda v: celaut.Amount()), \
                patch.object(cost_mod.BeeClient, "respond", side_effect=lambda message_iterator, indices: iter([message_iterator])), \
                patch.object(cost_mod.activity_window, "is_open", return_value=True):
            list(it.generate())
        self.assertEqual(refused, [True])


@unittest.skipIf(
    IMPORT_ERROR is not None or RUNTIME_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or RUNTIME_IMPORT_ERROR}",
)
class ResourceAvailabilityRpcTests(unittest.TestCase):

    def setUp(self):
        self._node = _node()
        self.cache = self._node.__enter__()
        self.addCleanup(self._node.__exit__, None, None, None)
        self.window_open = True

    def _ask(self, mem=100):
        request = celaut.ArchitectureResources()
        request.resources.mem_limit = mem
        it = avail_mod.GetResourceAvailabilityIterable(iter([]), MagicMock())
        answers = []
        with patch.object(avail_mod, "parse_with_client", return_value=(request, "c")), \
                patch.object(avail_mod, "require_caller"), \
                patch.object(avail_mod.activity_window, "is_open", side_effect=lambda: self.window_open), \
                patch.object(avail_mod.activity_window, "closed_reason", return_value="closed"), \
                patch.object(avail_mod, "get_architecture_availability", return_value={"can_execute": True}) as local, \
                patch.object(avail_mod.BeeClient, "respond", side_effect=lambda message_iterator: answers.append(message_iterator) or iter([])):
            list(it)
        return answers[0], local

    def test_the_same_question_is_computed_once(self):
        _, first = self._ask()
        _, second = self._ask()
        self.assertEqual(first.call_count, 1)
        self.assertEqual(second.call_count, 0)

    def test_another_shape_is_computed_again(self):
        self._ask(100)
        _, local = self._ask(200)
        self.assertEqual(local.call_count, 1)

    def test_the_hours_win_over_a_remembered_answer(self):
        answer, _ = self._ask()
        self.assertTrue(answer.can_execute)
        self.window_open = False
        answer, local = self._ask()
        self.assertFalse(answer.can_execute)
        self.assertEqual(answer.reason, "closed")
        local.assert_not_called()
        self.window_open = True
        answer, _ = self._ask()
        self.assertTrue(answer.can_execute)  # the remembered answer was not overwritten

    def test_a_ttl_of_zero_turns_it_off(self):
        with patch.object(qc, "ttl_seconds", return_value=0):
            _, first = self._ask()
            _, second = self._ask()
        self.assertEqual((first.call_count, second.call_count), (1, 1))


@unittest.skipIf(
    IMPORT_ERROR is not None or RUNTIME_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or RUNTIME_IMPORT_ERROR}",
)
class AskingSideTests(unittest.TestCase):

    def setUp(self):
        self._node = _node()
        self.cache = self._node.__enter__()
        self.addCleanup(self._node.__exit__, None, None, None)

    def test_a_peers_availability_is_asked_once_per_question(self):
        request = celaut.ArchitectureResources()
        with patch("src.identity.grpc_transport.peer_channel"), \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="c"), \
                patch("src.utils.bee_client.BeeClient.get_resource_availability",
                      return_value=celaut.ResourceAvailability(can_execute=True)) as ask:
            first = admission_mod.check_resource_availability_on_peer("peer-a", request)
            second = admission_mod.check_resource_availability_on_peer("peer-a", request)
            other = admission_mod.check_resource_availability_on_peer("peer-b", request)
        self.assertEqual((first, second, other), (True, True, True))
        self.assertEqual(ask.call_count, 2)

    def test_a_peer_that_could_not_be_asked_is_not_remembered(self):
        request = celaut.ArchitectureResources()
        with patch("src.identity.grpc_transport.peer_channel"), \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="c"), \
                patch("src.utils.bee_client.BeeClient.get_resource_availability",
                      side_effect=[RuntimeError("down"), celaut.ResourceAvailability(can_execute=True)]) as ask:
            self.assertIsNone(admission_mod.check_resource_availability_on_peer("peer-a", request))
            self.assertIs(admission_mod.check_resource_availability_on_peer("peer-a", request), True)
        self.assertEqual(ask.call_count, 2)

    def test_a_peers_quote_is_asked_once_per_question(self):
        asked = []

        def _cost(channel, message_iterator, timeout=None):
            asked.append(1)
            quote = celaut.EstimatedCost()
            quote.cost.n = "7"
            return quote

        def estimate():
            return balancer_mod.estimate_cost_on_peer(
                peer_id="peer-a",
                resources=celaut.Service.Container.Resources(),
                metadata=celaut.Metadata(),
                configuration=celaut.Configuration(),
            )

        with patch.object(balancer_mod, "peer_channel"), \
                patch.object(balancer_mod, "get_client_id_on_other_peer", return_value="c"), \
                patch.object(balancer_mod, "matching_payment_system", return_value=object()), \
                patch.object(balancer_mod, "configuration_for_peer", side_effect=lambda c, **_: c), \
                patch.object(balancer_mod, "estimated_cost_for_local", side_effect=lambda e, **_: e), \
                patch.object(balancer_mod.BeeClient, "get_service_estimated_cost", side_effect=_cost):
            first = estimate()
            first.cost.n = "mutated by the caller"
            second = estimate()
        self.assertEqual(len(asked), 1)
        self.assertEqual(second.cost.n, "7")


if __name__ == "__main__":
    unittest.main()
