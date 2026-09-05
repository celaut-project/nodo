"""`nodo burnall` stops every instance, and the order is the point.

A running orchestrator launches children on demand. Stop its children first and
it replaces them while the loop is still going, so the node ends up running
services the loop already stopped once -- which is why this is a command with an
ordering rather than a shell loop over `nodo instances`.

That ordering is also what the money has to work around. `stop_instance` credits
a child's leftover to its father after purging the child's row, so a burn that
stopped the father first would be crediting a row that no longer exists and every
child's unspent deposit would vanish. `burnall` therefore stops with
`credit=False` and rolls the leftovers up its own parent tree, paying each total
to the client that asked for the root -- or to the nearest ancestor that would
not stop, which is still there to receive it.
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


def _burn(stop):
    """A confirmed burn of the whole `TREE`, driven by a per-token `stop_instance`.

    Returns the order the instances were stopped in and the credits that came out
    of it, keyed by the account each total was paid to.
    """
    with patch.object(burnall_cmd.os, "geteuid", return_value=0), \
         patch.object(burnall_cmd.sc, "get_all_internal_containers_ids",
                      return_value=list(TREE)), \
         patch.object(burnall_cmd.sc, "get_internal_father_id",
                      side_effect=lambda id: TREE.get(id, "")), \
         patch.object(burnall_cmd, "credit_father", return_value=True) as credit_mock, \
         patch.object(burnall_cmd, "stop_instance", side_effect=stop) as stop_mock:
        burnall_cmd.burnall(argv=["--yes"])
    return (
        [c.kwargs["token"] for c in stop_mock.call_args_list],
        {c.kwargs["father_id"]: c.kwargs["amount_mu"] for c in credit_mock.call_args_list},
    )

@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BurnallOrderingTests(unittest.TestCase):
    def _run(self, argv=None, euid=0, stop_result=1, ids=None, typed=burnall_cmd.CONFIRMATION,
             tty=True):
        ids = list(TREE) if ids is None else ids
        with patch.object(burnall_cmd.os, "geteuid", return_value=euid), \
             patch.object(burnall_cmd.sc, "get_all_internal_containers_ids", return_value=ids), \
             patch.object(burnall_cmd.sc, "get_internal_father_id",
                          side_effect=lambda id: TREE.get(id, "")), \
             patch.object(burnall_cmd.sys.stdin, "isatty", return_value=tty), \
             patch("builtins.input", return_value=typed), \
             patch.object(burnall_cmd, "credit_father", return_value=True), \
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

    def test_nothing_burns_without_the_exact_phrase(self):
        self.assertEqual(self._run(typed="yes"), [])
        self.assertEqual(self._run(typed="burn all"), [])
        self.assertEqual(self._run(typed=""), [])

    def test_the_phrase_is_accepted_with_surrounding_whitespace(self):
        order = self._run(typed=f"  {burnall_cmd.CONFIRMATION}  ")
        self.assertCountEqual(order, list(TREE))

    def test_a_closed_stdin_is_a_refusal_not_an_assent(self):
        # The one thing this must never do is read "nobody is there to answer"
        # as permission to empty the node.
        self.assertEqual(self._run(tty=False), [])

    def test_yes_skips_the_prompt(self):
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            order = self._run(argv=["--yes"], typed=None)
        self.assertCountEqual(order, list(TREE))

    def test_the_prompt_reports_how_many_a_client_asked_for(self):
        # `parent` and `other-root` are the client-owned roots; a burn is felt
        # outside this node through those.
        with patch.object(burnall_cmd, "_confirmed", return_value=False) as confirm:
            self._run(typed=None)
        confirm.assert_called_once_with(total=4, roots=2)

    def test_the_throne_is_shown_before_anything_is_stopped(self):
        # An operator gets one look at the scale of this before being asked.
        printed = []
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            self._run(typed="no")
        art = "\n".join(printed)
        self.assertIn("B U R N", art)
        self.assertIn("\u2593", art)  # the throne's blades

    def test_one_instance_that_will_not_stop_does_not_strand_the_rest(self):
        # The instances after it in the order are its children, which are
        # exactly what a burnall is for.
        def stop(token, credit=True):
            if token == "child":
                raise RuntimeError("busy")
            return 1

        attempted, _ = _burn(stop)
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


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RefundRollupTests(unittest.TestCase):
    """Where the leftovers land when the fathers they belong to are gone.

    The father of every stopped instance in `TREE` is purged before its children
    are reached, so nothing here can be credited instance by instance. What is
    under test is that no MU is dropped on the way up: the whole subtree's
    leftovers reach the client that asked for the root.
    """

    LEFTOVERS = {"parent": 10, "child": 20, "grandchild": 30, "other-root": 40}

    def test_nothing_is_credited_to_a_father_this_burn_already_purged(self):
        # The bug: `stop_instance`'s own credit would hand `child`'s leftover to
        # `parent`, whose row is gone by then, and the money would be dropped.
        _, credits = _burn(lambda token, credit=True: self.LEFTOVERS[token])
        self.assertNotIn("parent", credits)
        self.assertNotIn("child", credits)

    def test_a_whole_subtree_is_refunded_to_the_client_that_asked_for_its_root(self):
        _, credits = _burn(lambda token, credit=True: self.LEFTOVERS[token])
        self.assertEqual(credits, {"client-1": 10 + 20 + 30, "client-2": 40})

    def test_the_leftovers_are_read_without_being_credited_twice(self):
        # `credit=False` is what makes the roll-up the only payment: leave it out
        # and `stop_instance` pays the father as well.
        with patch.object(burnall_cmd.os, "geteuid", return_value=0), \
             patch.object(burnall_cmd.sc, "get_all_internal_containers_ids",
                          return_value=list(TREE)), \
             patch.object(burnall_cmd.sc, "get_internal_father_id",
                          side_effect=lambda id: TREE.get(id, "")), \
             patch.object(burnall_cmd, "credit_father", return_value=True), \
             patch.object(burnall_cmd, "stop_instance", return_value=1) as stop_mock:
            burnall_cmd.burnall(argv=["--yes"])
        self.assertTrue(all(c.kwargs["credit"] is False for c in stop_mock.call_args_list))

    def test_an_ancestor_that_would_not_stop_is_paid_directly(self):
        # `parent` is still running, so it still has a row and the money is still
        # its own: crediting its client instead would fund the client out of a
        # live instance's balance.
        def stop(token, credit=True):
            if token == "parent":
                raise RuntimeError("busy")
            return self.LEFTOVERS[token]

        _, credits = _burn(stop)
        self.assertEqual(credits, {"parent": 20 + 30, "client-2": 40})

    def test_an_instance_that_reported_no_result_owes_nothing(self):
        # A failed purge leaves the balance on the row, so there is nothing to
        # roll up -- and crediting anyway would invent MU nobody paid in.
        def stop(token, credit=True):
            return None if token == "other-root" else self.LEFTOVERS[token]

        _, credits = _burn(stop)
        self.assertEqual(credits, {"client-1": 10 + 20 + 30})
