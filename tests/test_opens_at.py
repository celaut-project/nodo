"""`ResourceAvailability.opens_at`: the next opening, given when the node is closed (#344)."""
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.gateway.iterables import resource_availability_iterable as avail_mod
    from src.utils.tools.query_cache import QueryCache
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OpensAtTests(unittest.TestCase):
    def _ask(self, *, is_open, executes=True, opens_at=1_800_000_000):
        answers = []
        iterable = avail_mod.GetResourceAvailabilityIterable(iter([]), MagicMock())
        with patch.object(avail_mod, "parse_with_client",
                          return_value=(celaut.ResourceAvailabilityRequest(), "c")), \
                patch.object(avail_mod, "require_caller"), \
                patch.object(avail_mod, "executes_locally", return_value=executes), \
                patch.object(avail_mod.QueryCache, "get_or_compute",
                             side_effect=lambda key, ttl, compute: {"can_execute": True}), \
                patch.object(avail_mod.activity_window, "is_open", return_value=is_open), \
                patch.object(avail_mod.activity_window, "closed_reason", return_value="closed"), \
                patch.object(avail_mod.activity_window, "next_open", return_value=opens_at), \
                patch.object(avail_mod.BeeClient, "respond",
                             side_effect=lambda message_iterator: answers.append(message_iterator) or iter([])):
            list(iterable)
        return answers[0]

    def test_a_closed_node_says_no_and_when_it_opens(self):
        answer = self._ask(is_open=False)
        self.assertFalse(answer.can_execute)
        self.assertEqual(answer.opens_at, 1_800_000_000)

    def test_an_open_node_gives_zero(self):
        answer = self._ask(is_open=True)
        self.assertTrue(answer.can_execute)
        self.assertEqual(answer.opens_at, 0)

    def test_a_cause_that_is_not_the_window_gives_zero(self):
        answer = self._ask(is_open=False, executes=False)
        self.assertFalse(answer.can_execute)
        self.assertEqual(answer.opens_at, 0)

    def test_a_closed_node_with_no_opening_to_give_says_zero(self):
        self.assertEqual(self._ask(is_open=False, opens_at=0).opens_at, 0)


if __name__ == "__main__":
    unittest.main()
