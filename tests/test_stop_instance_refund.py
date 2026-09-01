"""What happens to a child's unspent deposit when the child is stopped.

The node charges a father the child's whole ``initial_mu`` when the child starts
(``modify_deposit`` -> ``spend_mu``). The child then spends that balance one
maintenance tick at a time. Whatever is left when it stops is the father's money
and has to go back to the father -- otherwise a parent that starts and stops
children in a loop pays a full deposit per child per iteration and is refunded
nothing, so its balance falls at the rate it *provisions* rather than at the rate
anything is *consumed*. That is unbounded: no amount of funding survives it.

``stop_instance`` already read the leftover into ``refund`` and already returned it
to its caller, as though the credit were happening; only the credit itself was
missing, and ``purge_internal``'s DELETE then dropped the row it was still sitting
in.

The credit is issued after that DELETE, and the ordering is load-bearing in the
opposite direction to the intuitive one: a stop that fails part-way is retried on
the next maintenance tick, so a credit issued before the DELETE is issued again
every time the DELETE does not happen. See
``test_a_purge_that_fails_credits_nothing_so_a_retry_cannot_pay_twice``.

These tests stop at the database boundary on purpose: what is under test is that
the money is handed to the right account, in the right amount, at the right point
in the sequence -- not what SQLite does with it afterwards.
"""

import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    import src.manager.manager as manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    manager = None  # type: ignore[assignment]


CHILD = "child-instance-id"
FATHER_INSTANCE = "father-instance-id"
FATHER_CLIENT = "dev-father-client-id"
LEFTOVER = 95_000_000


class _Harness:
    """A stopped internal instance with ``LEFTOVER`` MU still on its row."""

    def __init__(self, father_id, father_is_instance, purge_raises=False):
        self.sc = MagicMock()
        self.father_id = father_id
        self.purge_raises = purge_raises
        self.calls = []

        self.sc.internal_instance_exists.side_effect = lambda id: (
            id == CHILD or (father_is_instance and id == father_id)
        )
        self.sc.client_exists.side_effect = lambda client_id: (
            not father_is_instance and client_id == father_id
        )
        self.sc.get_sys_req.return_value = {"mem_limit": 0}
        self.sc.get_internal_father_id.return_value = father_id
        self.sc.get_internal_instance.return_value = None
        self.sc.get_instance_balance.side_effect = self._balance
        self.sc.update_instance_balance.side_effect = self._record_instance_credit
        self.sc.add_balance.side_effect = self._record_client_credit
        self.sc.purge_internal.side_effect = self._record_purge
        self.balances = {CHILD: LEFTOVER, father_id: 0}

    def _balance(self, id):
        return self.balances.get(id, 0)

    def _record_instance_credit(self, id, balance_mu):
        self.calls.append(("credit_instance", id, balance_mu))
        self.balances[id] = balance_mu

    def _record_client_credit(self, client_id, balance_mu):
        self.calls.append(("credit_client", client_id, balance_mu))
        self.balances[client_id] = self.balances.get(client_id, 0) + balance_mu

    def _record_purge(self, id):
        self.calls.append(("purge", id))
        if self.purge_raises:
            raise RuntimeError("database is locked")

    def run(self):
        with patch.object(manager, "sc", self.sc), \
                patch.object(manager, "kill", return_value=True), \
                patch.object(manager, "resolve_instance_token", return_value=None), \
                patch.object(manager.IOBigData, "log_snapshot", lambda *a, **k: None):
            return manager.stop_instance(token=CHILD)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StopInstanceReturnsTheUnspentDepositTests(unittest.TestCase):

    def test_an_instance_father_is_credited_what_the_child_did_not_spend(self):
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True)
        refund = harness.run()

        self.assertEqual(refund, LEFTOVER)
        self.assertIn(("credit_instance", FATHER_INSTANCE, LEFTOVER), harness.calls)

    def test_a_client_father_is_credited_too(self):
        """A top-level instance's father is a client row, not an instance.

        Both kinds have to be handled or the refund is dropped for one of them --
        and a client father is the ordinary case for anything launched with
        `nodo execute`.
        """
        harness = _Harness(FATHER_CLIENT, father_is_instance=False)
        refund = harness.run()

        self.assertEqual(refund, LEFTOVER)
        self.assertIn(("credit_client", FATHER_CLIENT, LEFTOVER), harness.calls)

    def test_the_credit_happens_after_the_row_is_deleted(self):
        """Order matters, and not in the direction it first looks.

        The amount is already in hand -- `refund` is a local read taken before
        anything is deleted -- so crediting first buys nothing. What the ordering
        decides is what a *retry* does, and stops are retried: `maintain_vmachines`
        calls `stop_instance` again on the next tick for an instance still on the
        books. Credit first and a purge that fails hands the father the same
        leftover on every tick thereafter.
        """
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True)
        harness.run()

        kinds = [call[0] for call in harness.calls]
        self.assertIn("credit_instance", kinds)
        self.assertIn("purge", kinds)
        self.assertLess(kinds.index("purge"), kinds.index("credit_instance"))

    def test_a_purge_that_fails_credits_nothing_so_a_retry_cannot_pay_twice(self):
        """A stop that does not delete the row must not have moved any money.

        The row survives with its balance intact and the next maintenance tick
        stops the instance again. Anything credited on the way through would be
        credited again from a balance nobody ever spent -- MU invented by a retry
        loop. Of the two ways to be wrong here, owing a father his leftover is
        recoverable and printing MU is not.
        """
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True, purge_raises=True)
        self.assertIsNone(harness.run())

        self.assertIn(("purge", CHILD), harness.calls)
        self.assertEqual(
            [call for call in harness.calls if call[0].startswith("credit")], []
        )
        self.assertEqual(harness.balances[FATHER_INSTANCE], 0)

    def test_an_exhausted_child_credits_nothing(self):
        """A child that spent its whole deposit must not manufacture MU."""
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True)
        harness.balances[CHILD] = 0
        harness.run()

        self.assertEqual(
            [call for call in harness.calls if call[0].startswith("credit")], []
        )

    def test_a_child_with_no_father_is_still_purged(self):
        """Missing father: nothing to credit, but the stop must still complete.

        Refusing to purge would leak the instance row and the manager would try to
        stop it again on every tick.
        """
        harness = _Harness("", father_is_instance=False)
        harness.sc.get_internal_father_id.return_value = ""
        harness.run()

        self.assertIn(("purge", CHILD), harness.calls)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CreditFatherTests(unittest.TestCase):
    """The refund primitive itself, used by both `stop_instance` and `modify_deposit`."""

    def _sc(self, *, instance=False, client=False):
        sc = MagicMock()
        sc.internal_instance_exists.return_value = instance
        sc.client_exists.return_value = client
        sc.get_instance_balance.return_value = 10
        return sc

    def test_an_instance_father_gets_its_existing_balance_plus_the_refund(self):
        sc = self._sc(instance=True)
        with patch.object(manager, "sc", sc):
            self.assertTrue(manager.credit_father(father_id="f", amount_mu=5))
        sc.update_instance_balance.assert_called_once_with(id="f", balance_mu=15)

    def test_a_client_father_is_credited_the_refund(self):
        sc = self._sc(client=True)
        with patch.object(manager, "sc", sc):
            self.assertTrue(manager.credit_father(father_id="f", amount_mu=5))
        sc.add_balance.assert_called_once_with(client_id="f", balance_mu=5)

    def test_an_unknown_father_is_reported_rather_than_silently_swallowed(self):
        sc = self._sc()
        with patch.object(manager, "sc", sc):
            self.assertFalse(manager.credit_father(father_id="nobody", amount_mu=5))
        sc.update_instance_balance.assert_not_called()
        sc.add_balance.assert_not_called()

    def test_nothing_to_refund_touches_no_account(self):
        sc = self._sc(instance=True)
        with patch.object(manager, "sc", sc):
            self.assertTrue(manager.credit_father(father_id="f", amount_mu=0))
        sc.update_instance_balance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
