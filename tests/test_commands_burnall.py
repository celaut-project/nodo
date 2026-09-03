"""`nodo burnall` stops every instance, and the order is the point.

A running orchestrator launches children on demand. Stop its children first and
it replaces them while the loop is still going, so the node ends up running
services the loop already stopped once -- which is why this is a command with an
ordering rather than a shell loop over `nodo instances`.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.commands import burnall as burnall_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    burnall_cmd = None  # type: ignore[assignment]


# parent <- child <- grandchild, plus an unrelated root. Declared out of order,
# because the id list arrives in whatever order the table yields.
TREE = {
    "grandchild": "child",
    "child": "parent",
    "parent": "client-1",   # a client, not an instance: this is a root
    "other-root": "client-2",
}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BurnallOrderingTests(unittest.TestCase):
    def _run(self, argv=None, euid=0, stop_result=1, ids=None):
        ids = list(TREE) if ids is None else ids
        with patch.object(burnall_cmd.os, "geteuid", return_value=euid), \
             patch.object(burnall_cmd.sc, "get_all_internal_containers_ids", return_value=ids), \
             patch.object(burnall_cmd.sc, "get_internal_father_id",
                          side_effect=lambda id: TREE.get(id, "")), \
             patch.object(burnall_cmd, "stop_instance", return_value=stop_result) as stop_mock:
            burnall_cmd.burnall(argv=argv)
        return [c.kwargs["token"] for c in stop_mock.call_args_list]

    def test_parents_are_stopped_before_their_children(self):
        order = self._run()
        self.assertEqual(order.index("parent"), 0 if order[0] == "parent" else order.index("parent"))
        self.assertLess(order.index("parent"), order.index("child"))
        self.assertLess(order.index("child"), order.index("grandchild"))

    def test_every_instance_is_stopped_exactly_once(self):
        order = self._run()
        self.assertCountEqual(order, list(TREE))

    def test_dry_run_stops_nothing(self):
        self.assertEqual(self._run(argv=["--dry-run"]), [])

    def test_a_dry_run_needs_no_privileges(self):
        # Reading the plan is harmless; only the stopping needs root.
        self.assertEqual(self._run(argv=["--dry-run"], euid=1000), [])

    def test_stopping_without_privileges_stops_nothing(self):
        self.assertEqual(self._run(euid=1000), [])

    def test_an_unrecognised_flag_is_refused_rather_than_ignored(self):
        # A typo must not silently become a full burn.
        self.assertEqual(self._run(argv=["--dry-runn"]), [])

    def test_an_empty_node_is_not_an_error(self):
        self.assertEqual(self._run(ids=[]), [])

    def test_one_instance_that_will_not_stop_does_not_strand_the_rest(self):
        # The instances after it in the order are its children, which are
        # exactly what a burnall is for.
        def stop(token):
            if token == "child":
                raise RuntimeError("busy")
            return 1

        with patch.object(burnall_cmd.os, "geteuid", return_value=0), \
             patch.object(burnall_cmd.sc, "get_all_internal_containers_ids",
                          return_value=list(TREE)), \
             patch.object(burnall_cmd.sc, "get_internal_father_id",
                          side_effect=lambda id: TREE.get(id, "")), \
             patch.object(burnall_cmd, "stop_instance", side_effect=stop) as stop_mock:
            burnall_cmd.burnall(argv=[])
        attempted = [c.kwargs["token"] for c in stop_mock.call_args_list]
        self.assertCountEqual(attempted, list(TREE))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepthTests(unittest.TestCase):
    def test_a_client_owned_instance_is_a_root(self):
        self.assertEqual(burnall_cmd._depth("parent", TREE, list(TREE)), 0)

    def test_depth_counts_internal_ancestors_only(self):
        self.assertEqual(burnall_cmd._depth("grandchild", TREE, list(TREE)), 2)

    def test_a_father_cycle_terminates(self):
        # Nothing has been stopped yet when this runs, so a spin here would hang
        # the command before it did any work.
        cyclic = {"a": "b", "b": "a"}
        self.assertIsInstance(burnall_cmd._depth("a", cyclic, ["a", "b"]), int)
