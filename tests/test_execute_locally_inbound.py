"""A node with `network.EXECUTE_LOCALLY: false` must not say it has room it will not use.

The announcement already carries no resources (`announced_resources`), but a peer that
asks anyway -- one that never read the announcement, or one holding an old copy of it --
used to be told what this machine could do, and its balancer then selected a node that
`launch_service` had no local candidate to run the work on. Pinned here:

- ``GetServiceEstimatedCost`` is refused, without the service being read or priced.
- ``GetResourceAvailability`` answers no, without the machine being measured, and a
  "yes" remembered from before the key was turned off is not served.
- The workload admission does not count this host's room.
- The failure of a launch does not blame a "local preflight" that never ran.

Each has a guard test with the key on, so the "off" case cannot pass for any reason.
"""
import contextlib
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.utils.singleton import Singleton
    from src.utils.tools.query_cache import QueryCache
    from src.utils.cost_functions import workload_admission as admission_mod
    from src.utils.cost_functions.architecture_resources import NOT_EXECUTING_REASON
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

GATEWAY_IMPORT_ERROR = None
try:
    from src.gateway.iterables import estimated_cost_iterable as cost_mod
    from src.gateway.iterables import resource_availability_iterable as avail_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    GATEWAY_IMPORT_ERROR = import_exc

LAUNCH_IMPORT_ERROR = None
try:
    from src.gateway.launcher import launch_service as launch_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    LAUNCH_IMPORT_ERROR = import_exc


@contextlib.contextmanager
def _fresh_cache():
    """Run the block with a node of its own: an empty `QueryCache`."""
    previous = Singleton._instances.pop(QueryCache, None)
    try:
        yield QueryCache()
    finally:
        if previous is None:
            Singleton._instances.pop(QueryCache, None)
        else:
            Singleton._instances[QueryCache] = previous


@unittest.skipIf(
    IMPORT_ERROR is not None or GATEWAY_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or GATEWAY_IMPORT_ERROR}",
)
class QuoteWhileNotExecutingTests(unittest.TestCase):

    def setUp(self):
        manager = _fresh_cache()
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.executes = True
        # These tests are about the local price. With delegation on, a node that does
        # not execute asks its peers instead (#551): tests/test_delegated_quote.py.
        self.delegates = False

    def _quote(self):
        it = cost_mod.GetServiceEstimatedCostIterable.__new__(cost_mod.GetServiceEstimatedCostIterable)
        it.configuration = celaut.Configuration()
        it.service_hash = "abc123"
        it.metadata = celaut.Metadata()
        it.recursion_guard_token = None
        it.recursion_guard_hops = None
        with patch.object(cost_mod, "executes_locally", side_effect=lambda: self.executes), \
                patch.object(cost_mod, "read_service_from_disk", return_value=celaut.Service()) as read, \
                patch.object(cost_mod, "enforce_network_policy"), \
                patch.object(cost_mod, "default_initial_balance", return_value=1), \
                patch.object(cost_mod, "generate_estimated_cost",
                             side_effect=lambda **_: celaut.EstimatedCost()) as price, \
                patch.object(cost_mod, "get_resource_availability",
                             return_value={"can_execute": True}) as room, \
                patch.object(cost_mod.BeeClient, "respond",
                             side_effect=lambda message_iterator, indices: iter([message_iterator])), \
                patch.object(cost_mod.activity_window, "is_open", return_value=True), \
                patch.object(cost_mod, "delegation_time_budget", return_value=None), \
                patch.object(cost_mod.env_manager, "get",
                             side_effect=lambda k, d=None: self.delegates if k == "network.DELEGATE_EXECUTION" else d), \
                patch.object(cost_mod, "to_amount", side_effect=lambda v: celaut.Amount()):
            self.read, self.price, self.room = read, price, room
            return list(it.generate())

    def test_a_node_that_executes_quotes(self):
        # Guards the test itself.
        self.assertIsInstance(self._quote()[0], celaut.EstimatedCost)

    def test_a_node_that_does_not_execute_refuses_the_quote_and_says_why(self):
        self.executes = False
        with self.assertRaises(Exception) as ctx:
            self._quote()
        self.assertEqual(str(ctx.exception), NOT_EXECUTING_REASON)
        self.assertIn("EXECUTE_LOCALLY", str(ctx.exception))

    def test_it_does_not_read_price_or_measure_anything_to_refuse(self):
        self.executes = False
        with self.assertRaises(Exception):
            self._quote()
        self.read.assert_not_called()
        self.price.assert_not_called()
        self.room.assert_not_called()

    def test_a_quote_remembered_from_before_the_key_was_turned_off_is_not_served(self):
        self._quote()
        self.executes = False
        with self.assertRaises(Exception) as ctx:
            self._quote()
        self.assertEqual(str(ctx.exception), NOT_EXECUTING_REASON)

    def test_turning_the_key_back_on_quotes_again(self):
        self.executes = False
        with self.assertRaises(Exception):
            self._quote()
        self.executes = True
        self.assertIsInstance(self._quote()[0], celaut.EstimatedCost)


@unittest.skipIf(
    IMPORT_ERROR is not None or GATEWAY_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or GATEWAY_IMPORT_ERROR}",
)
class AvailabilityWhileNotExecutingTests(unittest.TestCase):

    def setUp(self):
        manager = _fresh_cache()
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.executes = True
        self.window_open = True

    def _ask(self):
        request = celaut.ArchitectureResources()
        request.resources.mem_limit = 100
        it = avail_mod.GetResourceAvailabilityIterable(iter([]), MagicMock())
        answers = []
        with patch.object(avail_mod, "executes_locally", side_effect=lambda: self.executes), \
                patch.object(avail_mod, "parse_with_client", return_value=(request, "c")), \
                patch.object(avail_mod, "require_caller"), \
                patch.object(avail_mod.activity_window, "is_open",
                             side_effect=lambda: self.window_open), \
                patch.object(avail_mod.activity_window, "closed_reason", return_value="closed"), \
                patch.object(avail_mod, "get_architecture_availability",
                             return_value={"can_execute": True}) as measure, \
                patch.object(avail_mod.BeeClient, "respond",
                             side_effect=lambda message_iterator: answers.append(message_iterator) or iter([])):
            list(it)
        return answers[0], measure

    def test_a_node_that_executes_has_room(self):
        # Guards the test itself.
        answer, _ = self._ask()
        self.assertTrue(answer.can_execute)

    def test_a_node_that_does_not_execute_has_none_and_says_why(self):
        self.executes = False
        answer, _ = self._ask()
        self.assertFalse(answer.can_execute)
        self.assertEqual(answer.reason, NOT_EXECUTING_REASON)

    def test_the_machine_is_not_measured_to_say_no(self):
        self.executes = False
        _, measure = self._ask()
        measure.assert_not_called()

    def test_a_yes_remembered_from_before_is_not_served_and_is_not_lost(self):
        answer, _ = self._ask()
        self.assertTrue(answer.can_execute)
        self.executes = False
        answer, _ = self._ask()
        self.assertFalse(answer.can_execute)
        self.executes = True
        answer, measure = self._ask()
        self.assertTrue(answer.can_execute)
        measure.assert_not_called()  # the remembered answer was not overwritten

    def test_the_reason_is_not_the_hours_when_both_apply(self):
        # Closed hours are temporary; not executing is not. The reason says which.
        self.executes = False
        self.window_open = False
        answer, _ = self._ask()
        self.assertEqual(answer.reason, NOT_EXECUTING_REASON)

    def test_the_hours_still_apply_to_a_node_that_executes(self):
        self.window_open = False
        answer, _ = self._ask()
        self.assertFalse(answer.can_execute)
        self.assertEqual(answer.reason, "closed")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AdmissionWhileNotExecutingTests(unittest.TestCase):

    EXECUTES = "src.utils.cost_functions.architecture_resources.executes_locally"

    def _satisfiable(self, executes: bool, local_room: bool, delegate: bool = False):
        settings = {"network.DELEGATE_EXECUTION": delegate}
        with patch(self.EXECUTES, return_value=executes), \
                patch.object(admission_mod.env_manager, "get",
                             side_effect=lambda key, default=None: settings.get(key, default)), \
                patch.object(admission_mod, "_local_resource_availability",
                             return_value={"can_execute": local_room}) as local:
            result = admission_mod._workload_group_is_satisfiable(
                celaut.ArchitectureResources(), None
            )
        return result, local

    def test_a_node_that_executes_counts_its_own_room(self):
        result, _ = self._satisfiable(executes=True, local_room=True)
        self.assertTrue(result)

    def test_a_node_that_does_not_execute_does_not(self):
        result, local = self._satisfiable(executes=False, local_room=True)
        self.assertFalse(result)
        # Not even measured: its machine is not on offer.
        local.assert_not_called()

    def test_without_room_a_node_that_executes_is_refused_as_before(self):
        result, _ = self._satisfiable(executes=True, local_room=False)
        self.assertFalse(result)


@unittest.skipIf(
    IMPORT_ERROR is not None or LAUNCH_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or LAUNCH_IMPORT_ERROR}",
)
class PreflightWhileNotExecutingTests(unittest.TestCase):

    def _preflight(self, executes: bool):
        with patch.object(launch_mod, "executes_locally", return_value=executes), \
                patch.object(launch_mod, "get_arch_tag", return_value="x86_64"), \
                patch.object(launch_mod, "generate_estimated_cost", return_value=None) as price, \
                patch.object(launch_mod, "get_resource_availability",
                             return_value={"reason": "no room here"}):
            result = launch_mod._detect_local_preflight_failure(
                celaut.Service(), celaut.Metadata(), celaut.Configuration()
            )
        return result, price

    def test_a_node_that_executes_explains_a_local_refusal(self):
        # Guards the test itself.
        result, _ = self._preflight(executes=True)
        self.assertEqual(result, "no room here")

    def test_a_node_that_does_not_execute_blames_no_local_preflight(self):
        result, price = self._preflight(executes=False)
        self.assertIsNone(result)
        price.assert_not_called()


if __name__ == "__main__":
    unittest.main()
