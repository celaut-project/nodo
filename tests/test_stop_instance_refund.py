"""What happens to a child's unspent deposit when the child is stopped.

The node charges a father the child's whole ``initial_mu`` when the child starts
(``modify_deposit`` -> ``spend_mu``). The child then spends that balance one
maintenance tick at a time. Whatever is left when it stops is the father's money
and has to go back to the father -- otherwise a parent that starts and stops
children in a loop pays a full deposit per child per iteration and is refunded
nothing, so its balance falls at the rate it *provisions* rather than at the rate
anything is *consumed*. That is unbounded: no amount of funding survives it.

``stop_instance`` hands the leftover over in ``SQLConnection.settle_stop``: the refund
of the child, the refund of the father and the death of the child are one transaction,
so a stop that fails part-way moves nothing and its retry settles once. What the
transaction does with the rows is tested against a real database in
``test_balance_ledger.SettleStopTests``. These tests stop at that boundary: that
``stop_instance`` asks for the settlement with the right account, father and credit,
after the instance is killed, and returns what the settlement says.
"""

import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    import src.manager.manager as manager
    from src.database import ledger
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    manager = None  # type: ignore[assignment]


CHILD = "child-instance-id"
FATHER_INSTANCE = "father-instance-id"
FATHER_CLIENT = "dev-father-client-id"
LEFTOVER = 95_000_000


class _Harness:
    """A stopped internal instance with ``LEFTOVER`` MU still on its row."""

    def __init__(self, father_id, father_is_instance, settle_raises=False, leftover=LEFTOVER):
        self.sc = MagicMock()
        self.father_id = father_id
        self.settle_raises = settle_raises
        self.leftover = leftover
        self.calls = []

        self.sc.internal_instance_exists.side_effect = lambda id: (
            id == CHILD or (father_is_instance and id == father_id)
        )
        self.sc.get_sys_req.return_value = {"mem_limit": 0}
        self.sc.get_internal_father_id.return_value = father_id
        self.sc.get_internal_instance.return_value = None
        self.sc.settle_stop.side_effect = self._settle

    def _settle(self, account_id, reason, delegated=False, father_id=None, credit=True):
        self.calls.append(("settle", account_id, delegated, father_id, credit))
        if self.settle_raises:
            raise RuntimeError("database is locked")
        return self.leftover

    def _kill(self, **kwargs):
        self.calls.append(("kill", kwargs["vmachine_id"]))
        return True

    def run(self, credit=True):
        with patch.object(manager, "sc", self.sc), \
                patch.object(manager, "kill", side_effect=self._kill), \
                patch.object(manager.dead_instances, "write_death_record"), \
                patch.object(manager, "resolve_instance_token", return_value=None), \
                patch.object(manager.IOBigData, "log_snapshot", lambda *a, **k: None):
            return manager.stop_instance(token=CHILD, credit=credit)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StopInstanceReturnsTheUnspentDepositTests(unittest.TestCase):

    def test_an_instance_father_is_settled_with_what_the_child_did_not_spend(self):
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True)
        refund = harness.run()

        self.assertEqual(refund, LEFTOVER)
        self.assertIn(("settle", CHILD, False, FATHER_INSTANCE, True), harness.calls)

    def test_a_client_father_is_settled_too(self):
        """A top-level instance's father is a client row, not an instance.

        Both kinds have to be handled or the refund is dropped for one of them --
        and a client father is the ordinary case for anything launched with
        `nodo execute`.
        """
        harness = _Harness(FATHER_CLIENT, father_is_instance=False)
        refund = harness.run()

        self.assertEqual(refund, LEFTOVER)
        self.assertIn(("settle", CHILD, False, FATHER_CLIENT, True), harness.calls)

    def test_the_settlement_happens_after_the_instance_is_killed(self):
        """The instance is stopped for good before its deposit is handed back."""
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True)
        harness.run()

        kinds = [call[0] for call in harness.calls]
        self.assertLess(kinds.index("kill"), kinds.index("settle"))

    def test_a_settlement_that_fails_returns_nothing_so_the_retry_settles_once(self):
        """The settlement is one transaction: a failure moved no money.

        The next maintenance tick stops the instance again, and nothing was credited
        the first time, so nothing is credited twice. Of the two ways to be wrong
        here, owing a father his leftover is recoverable and printing MU is not.
        """
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True, settle_raises=True)

        self.assertIsNone(harness.run())

    def test_an_instance_that_was_already_dead_is_not_settled_again(self):
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True)
        harness.sc.settle_stop.side_effect = lambda *a, **k: None

        self.assertIsNone(harness.run())

    def test_an_exhausted_child_returns_zero(self):
        """A child that spent its whole deposit must not manufacture MU."""
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True, leftover=0)

        self.assertEqual(harness.run(), 0)

    def test_a_caller_that_takes_the_leftover_says_so(self):
        harness = _Harness(FATHER_INSTANCE, father_is_instance=True)

        self.assertEqual(harness.run(credit=False), LEFTOVER)
        self.assertIn(("settle", CHILD, False, FATHER_INSTANCE, False), harness.calls)

    def test_a_child_with_no_father_is_still_settled(self):
        """Missing father: nothing to credit, but the stop must still complete.

        Refusing to settle would leak the instance row and the manager would try to
        stop it again on every tick.
        """
        harness = _Harness("", father_is_instance=False)
        harness.sc.get_internal_father_id.return_value = ""
        harness.run()

        self.assertIn(("settle", CHILD, False, "", True), harness.calls)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CreditFatherTests(unittest.TestCase):
    """The refund primitive itself, used by both `stop_instance` and `modify_deposit`."""

    def test_the_father_is_credited_with_a_row_that_names_the_child(self):
        sc = MagicMock()
        with patch.object(manager, "sc", sc):
            self.assertTrue(manager.credit_father(father_id="f", amount_mu=5,
                                                  instance_token="child"))
        sc.move_balance.assert_called_once_with("f", 5, ledger.REFUND, instance_token="child")

    def test_a_withdrawal_is_written_as_one(self):
        sc = MagicMock()
        with patch.object(manager, "sc", sc):
            self.assertTrue(manager.credit_father(father_id="f", amount_mu=5,
                                                  kind=ledger.WITHDRAWAL))
        sc.move_balance.assert_called_once_with("f", 5, ledger.WITHDRAWAL, instance_token=None)

    def test_an_unknown_father_is_reported_rather_than_silently_swallowed(self):
        sc = MagicMock()
        sc.move_balance.side_effect = ledger.NoSuchAccount("nobody")
        with patch.object(manager, "sc", sc):
            self.assertFalse(manager.credit_father(father_id="nobody", amount_mu=5))

    def test_nothing_to_refund_touches_no_account(self):
        sc = MagicMock()
        with patch.object(manager, "sc", sc):
            self.assertTrue(manager.credit_father(father_id="f", amount_mu=0))
        sc.move_balance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
