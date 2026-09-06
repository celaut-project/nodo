"""A delegated child's deposit, from the charge that opens it to the refund that closes it.

A child that runs on a peer is charged to its father exactly as a local one is: the
whole deposit, up front, at StartService. What was missing was everything after that
charge. The deposit landed nowhere -- ``delegated_instances`` had no balance column --
so nothing spent it down while the child ran and nothing handed back what was left
when it stopped. A parent that started and stopped delegated children in a loop paid a
full deposit per iteration and was refunded nothing, the same unbounded drain #297
measured on the local path.

The money was not destroyed, which is what made this hard to see: the peer *does*
return the unspent part, into the deposit this node holds there. It is lost from the
father, not from the network. So the fix is not to forward the peer's figure to the
father -- that is a different account, on a different scale, and this node's own
wholesale position with that peer. It is to give the delegated child the same kind of
local account a local child has:

* the father's deposit is parked on the delegation row, in our MU;
* the maintenance tick charges it what the peer's own meter says the child spent;
* the stop hands whatever is left back to the father, after the row is gone;
* the peer's own refund settles on the peer, and is only logged here.

These tests stop at the database boundary on purpose: what is under test is that the
money is moved to the right account, in the right amount, at the right point in the
sequence.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.gateway.launcher.delegate_execution import delegate_execution as delegate_mod
    from src.manager import maintain, manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    delegate_mod = None  # type: ignore[assignment]
    maintain = None  # type: ignore[assignment]
    manager = None  # type: ignore[assignment]


OUR_ALIAS = "hashed-external-token"
PEER_TOKEN = "token-as-the-peer-knows-it"
PEER_ID = "peer-a"
FATHER_CLIENT = "client-a"
LEFTOVER = 400_000


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegationOpensTheChildsLocalAccountTests(unittest.TestCase):
    """What `delegate_execution` writes on the row it creates."""

    def _delegate(self, *, initial_mu, peer_initial_mu=None):
        config = celaut.Configuration()
        if initial_mu is not None:
            config.initial_mu.n = str(initial_mu)
        peer_config = celaut.Configuration()
        if peer_initial_mu is not None:
            peer_config.initial_mu.n = str(peer_initial_mu)
        payment_system = SimpleNamespace(
            local_mu_per_unit=1_000_000_000, peer_mu_per_unit=2_000_000_000
        )
        instance = celaut.ServiceInstance(token=PEER_TOKEN)

        with patch.object(
            delegate_mod, "matching_payment_system", return_value=payment_system
        ), patch.object(
            delegate_mod, "configuration_for_peer", return_value=peer_config
        ), patch.object(
            delegate_mod, "balance_on_other_peer", return_value=10 ** 12
        ), patch.object(
            delegate_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(delegate_mod, "peer_channel"), patch.object(
            delegate_mod.celaut_pb2_grpc, "GatewayStub"
        ), patch.object(
            delegate_mod.bee, "client_grpc", return_value=iter([instance])
        ), patch.object(
            delegate_mod.delegated_endpoints, "should_tunnel", return_value=False
        ), patch.object(delegate_mod, "SQLConnection") as sql_connection:
            delegate_mod.delegate_execution(
                service_id="service-a",
                peer=PEER_ID,
                father_id=FATHER_CLIENT,
                cost=1_500_000,
                metadata=celaut.Metadata(),
                config=config,
                recursion_guard_token="token-a",
                refund_container=[],
            )
        return sql_connection.return_value.add_delegated_instance.call_args.kwargs

    def test_the_deposit_the_father_paid_is_parked_on_the_row(self):
        """In *our* MU -- the local configuration's figure, not the one that travelled.

        `configuration_for_peer` makes a peer-scaled copy for the wire; the father was
        charged in our scale and is refunded in our scale, so the row keeps ours.
        """
        written = self._delegate(initial_mu=1_000_000, peer_initial_mu=2_000_000)

        self.assertEqual(written["balance_mu"], 1_000_000)

    def test_the_same_deposit_is_marked_as_the_peer_will_count_it(self):
        """The figure that travelled, on the peer's scale, is the tick's starting mark.

        It has to be what the peer started the child with rather than a reading taken
        afterwards: anything the child spends between the launch and the first tick
        is still the client's to pay, and a mark set later would write it off.
        """
        written = self._delegate(initial_mu=1_000_000, peer_initial_mu=2_000_000)

        self.assertEqual(written["peer_balance_mu"], 2_000_000)

    def test_a_configuration_with_no_deposit_opens_an_empty_account(self):
        """Never the whole charge: the difference is the peer's one-off start charge.

        That part buys the launch itself and is gone, exactly as a local instance's
        start charge is. Parking all of it would refund a father for work really done.
        """
        written = self._delegate(initial_mu=None)

        self.assertEqual(written["balance_mu"], 0)
        self.assertEqual(written["peer_balance_mu"], 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedMaintenanceTickTests(unittest.TestCase):
    """The sweep that charges a delegated deposit for what the peer metered."""

    # The peer's MU are worth half of ours here, so a conversion that silently used
    # the wrong scale shows up as a factor of two rather than as the same number.
    PAYMENT_SYSTEM = SimpleNamespace(
        local_mu_per_unit=1_000_000_000, peer_mu_per_unit=2_000_000_000
    )

    def _row(self, **overrides):
        row = {
            'token': PEER_TOKEN,
            'id': OUR_ALIAS,
            'peer_id': PEER_ID,
            'father_id': FATHER_CLIENT,
            'serialized_instance': None,
            'balance_mu': 1_000_000,
            'peer_balance_mu': 2_000_000,
        }
        row.update(overrides)
        return row

    def _tick(self, row, *, peer_balance_now=1_400_000, peer_raises=None,
              peer_reachable=True, rates=True):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = [row]
        reading = (
            MagicMock(side_effect=peer_raises) if peer_raises
            else MagicMock(return_value=peer_balance_now)
        )
        with patch.object(maintain, "sc", sc), \
                patch.object(maintain, "instance_balance_on_peer", reading), \
                patch.object(maintain, "is_peer_available", return_value=peer_reachable), \
                patch.object(
                    maintain, "peer_mu_in_local",
                    side_effect=(self._convert if rates else lambda *a, **k: None)
                ), \
                patch.object(maintain, "stop_instance") as stop, \
                patch.object(maintain, "ALLOW_DEBT", False), \
                patch.object(maintain, "format_mu", str):
            maintain.maintain_delegated_instances()
        return sc, stop

    @staticmethod
    def _convert(peer_id, amount_mu, *, round_up=False):
        """The peer's MU in ours at PAYMENT_SYSTEM's rates, rounding as asked."""
        numerator = amount_mu * 1_000_000_000
        if round_up:
            return -(-numerator // 2_000_000_000)
        return numerator // 2_000_000_000

    def test_the_client_pays_what_the_peer_metered_since_the_last_reading(self):
        """600 000 of the peer's MU spent, worth 300 000 of ours."""
        sc, stop = self._tick(self._row(), peer_balance_now=1_400_000)

        sc.update_delegated_deposit.assert_called_once_with(
            token=PEER_TOKEN, balance_mu=700_000, peer_balance_mu=1_400_000
        )
        stop.assert_not_called()

    def test_the_mark_advances_in_the_same_write_as_the_charge(self):
        """Both or neither, and in one statement rather than two.

        Every `_execute` commits on its own, so a charge written and a mark left
        behind is a state a failure can leave: the next tick measures against the old
        mark and bills the client for the same consumption twice. The reverse order
        writes the consumption off instead. One write has neither failure.
        """
        sc, _ = self._tick(self._row(), peer_balance_now=1_400_000)

        sc.update_delegated_balance.assert_not_called()
        sc.update_delegated_peer_balance.assert_not_called()
        sc.update_delegated_deposit.assert_called_once_with(
            token=PEER_TOKEN, balance_mu=700_000, peer_balance_mu=1_400_000
        )

    def test_an_instance_that_cannot_pay_what_it_consumed_is_stopped(self):
        """The same reaper a local instance gets, and the row is emptied first.

        Every MU on it is already owed to the peer for runtime that happened, so
        letting `stop_instance` refund it would hand the father back time this node
        paid for. What the node absorbs is only the part the deposit could not
        reach.
        """
        sc, stop = self._tick(self._row(balance_mu=100), peer_balance_now=1_400_000)

        stop.assert_called_once_with(token=OUR_ALIAS)
        sc.update_delegated_balance.assert_called_once_with(token=PEER_TOKEN, balance_mu=0)
        sc.update_delegated_deposit.assert_not_called()

    def test_a_peer_that_cannot_be_reached_charges_nothing_and_loses_nothing(self):
        """The mark stays put, so the next reading that works charges the whole gap.

        A tick that charged a guess here, or advanced the mark without charging,
        would turn an unreachable peer into free runtime or into an invented bill.
        """
        sc, stop = self._tick(
            self._row(), peer_raises=RuntimeError("unreachable"), peer_reachable=False
        )

        sc.update_delegated_deposit.assert_not_called()
        sc.update_delegated_peer_balance.assert_not_called()
        stop.assert_not_called()

    def test_a_peer_that_no_longer_knows_the_instance_stops_it_here_too(self):
        """It is up and answering; the instance is simply gone there.

        Left alone, the row would keep a father's deposit hostage to an instance
        that no longer runs anywhere.
        """
        sc, stop = self._tick(
            self._row(), peer_raises=RuntimeError("unknown token"), peer_reachable=True
        )

        stop.assert_called_once_with(token=OUR_ALIAS)

    def test_a_child_that_holds_more_than_the_mark_is_only_re_marked(self):
        """A top-up between readings, not negative consumption.

        Crediting the difference would pay a client for MU he was already refunded
        on the local side of the same top-up.
        """
        sc, stop = self._tick(self._row(), peer_balance_now=3_000_000)

        sc.update_delegated_deposit.assert_not_called()
        sc.update_delegated_peer_balance.assert_called_once_with(
            token=PEER_TOKEN, peer_balance_mu=3_000_000
        )
        stop.assert_not_called()

    def test_no_common_payment_system_charges_nothing_rather_than_zero(self):
        """What the peer metered is real; what it is worth here is unknown.

        Charging zero would say the runtime was free, and advancing the mark would
        make that permanent. Both wait for a rate.
        """
        sc, stop = self._tick(self._row(), rates=False)

        sc.update_delegated_deposit.assert_not_called()
        sc.update_delegated_peer_balance.assert_not_called()
        stop.assert_not_called()

    def test_one_instance_that_will_not_stop_does_not_strand_the_rest(self):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = [
            self._row(id="wont-stop", balance_mu=0),
            self._row(token="second-token"),
        ]
        with patch.object(maintain, "sc", sc), \
                patch.object(maintain, "instance_balance_on_peer", return_value=1_400_000), \
                patch.object(maintain, "is_peer_available", return_value=True), \
                patch.object(maintain, "peer_mu_in_local", side_effect=self._convert), \
                patch.object(maintain, "stop_instance", side_effect=[RuntimeError("no"), None]), \
                patch.object(maintain, "ALLOW_DEBT", False), \
                patch.object(maintain, "format_mu", str):
            maintain.maintain_delegated_instances()

        sc.update_delegated_deposit.assert_called_once_with(
            token="second-token", balance_mu=700_000, peer_balance_mu=1_400_000
        )


class _StopHarness:
    """A delegated instance the peer agrees to stop, with `LEFTOVER` MU left here."""

    def __init__(self, *, father_id=FATHER_CLIENT, father_is_instance=False,
                 leftover=LEFTOVER, peer_refund=999_999_999, purge_raises=False,
                 peer_raises=False):
        self.sc = MagicMock()
        self.calls = []
        self.peer_raises = peer_raises

        self.sc.internal_instance_exists.side_effect = lambda id: (
            father_is_instance and id == father_id
        )
        self.sc.client_exists.side_effect = lambda client_id: (
            not father_is_instance and client_id == father_id
        )
        self.sc.get_delegated_token_by_id.return_value = PEER_TOKEN
        self.sc.get_peer_id_by_external_service.return_value = PEER_ID
        self.sc.get_delegated_balance.return_value = leftover
        self.sc.get_external_father_id.return_value = father_id
        self.sc.get_delegated_instance.return_value = None
        self.sc.get_instance_balance.return_value = 0
        self.sc.purgue_delegated.side_effect = self._record_purge
        self.sc.add_balance.side_effect = self._record_client_credit
        self.sc.update_instance_balance.side_effect = self._record_instance_credit
        self.purge_raises = purge_raises

        self.peer_refund = celaut.Refund(amount=celaut.Amount(n=str(peer_refund)))

    def _record_purge(self, token):
        self.calls.append(("purge", token))
        if self.purge_raises:
            raise RuntimeError("database is locked")

    def _record_client_credit(self, client_id, balance_mu):
        self.calls.append(("credit_client", client_id, balance_mu))

    def _record_instance_credit(self, id, balance_mu):
        self.calls.append(("credit_instance", id, balance_mu))

    def _stop_service(self, **kwargs):
        self.calls.append(("stop_service_on_peer", PEER_TOKEN))
        if self.peer_raises:
            raise RuntimeError("peer is unreachable")
        return iter([self.peer_refund])

    def run(self, credit=True):
        with patch.object(manager, "sc", self.sc), \
                patch.object(manager, "resolve_instance_token", return_value=None), \
                patch.object(manager, "format_mu", str), \
                patch.object(manager.utils, "generate_uris_by_peer_id",
                             return_value=iter(["peer:5000"])), \
                patch.object(manager, "node_channel"), \
                patch.object(manager.celaut_pb2_grpc, "GatewayStub"), \
                patch.object(manager.bee, "client_grpc", side_effect=self._stop_service), \
                patch.object(manager.delegated_endpoints, "close") as close:
            self.close = close
            return manager.stop_instance(token=OUR_ALIAS, credit=credit)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedStopReturnsTheDepositTests(unittest.TestCase):

    def test_the_father_gets_back_what_is_left_of_the_deposit_he_paid(self):
        harness = _StopHarness()
        refund = harness.run()

        self.assertEqual(refund, LEFTOVER)
        self.assertIn(("credit_client", FATHER_CLIENT, LEFTOVER), harness.calls)

    def test_the_peers_own_refund_is_not_credited_to_the_father(self):
        """It is a different account, in a different unit.

        The peer credits it to the client row this node holds there -- this node's
        wholesale deposit with that peer, in the peer's MU. Handing that figure to a
        local father would pay him out of an account that is not his, at a scale that
        is not his, on top of the deposit he is already owed. The harness makes them
        wildly different on purpose.
        """
        harness = _StopHarness(peer_refund=999_999_999)
        refund = harness.run()

        self.assertEqual(refund, LEFTOVER)
        credits = [c for c in harness.calls if c[0].startswith("credit")]
        self.assertEqual(credits, [("credit_client", FATHER_CLIENT, LEFTOVER)])

    def test_an_instance_father_is_credited_too(self):
        harness = _StopHarness(father_id="father-instance", father_is_instance=True)
        harness.run()

        self.assertIn(("credit_instance", "father-instance", LEFTOVER), harness.calls)

    def test_the_credit_happens_after_the_delegation_row_is_deleted(self):
        """The ordering lesson from #297, on this branch too.

        A stop that fails part-way is retried, and a credit issued before the row is
        gone is issued again on every retry -- MU manufactured out of a deposit
        nobody spent.
        """
        harness = _StopHarness()
        harness.run()

        kinds = [call[0] for call in harness.calls]
        self.assertLess(kinds.index("purge"), kinds.index("credit_client"))

    def test_a_purge_that_fails_credits_nothing_so_a_retry_cannot_pay_twice(self):
        harness = _StopHarness(purge_raises=True)

        self.assertIsNone(harness.run())
        self.assertEqual(
            [c for c in harness.calls if c[0].startswith("credit")], []
        )

    def test_a_peer_that_will_not_confirm_the_stop_settles_nothing(self):
        """Neither the row nor the money moves while the instance may still be running.

        The next tick tries again. Tunnel endpoints are ours alone, so those come
        down either way.
        """
        harness = _StopHarness(peer_raises=True)

        self.assertIsNone(harness.run())
        self.assertEqual([c for c in harness.calls if c[0] == "purge"], [])
        self.assertEqual([c for c in harness.calls if c[0].startswith("credit")], [])
        harness.close.assert_called_once_with(token=PEER_TOKEN)

    def test_a_caller_that_took_the_leftover_on_itself_is_not_second_guessed(self):
        harness = _StopHarness()
        refund = harness.run(credit=False)

        self.assertEqual(refund, LEFTOVER)
        self.assertEqual(
            [c for c in harness.calls if c[0].startswith("credit")], []
        )

    def test_an_exhausted_child_credits_nothing(self):
        harness = _StopHarness(leftover=0)
        refund = harness.run()

        self.assertEqual(refund, 0)
        self.assertEqual(
            [c for c in harness.calls if c[0].startswith("credit")], []
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedDepositModificationTests(unittest.TestCase):
    """`modify_deposit` moves both halves: the local row and the peer's own deposit."""

    def _modify(self, amount_mu, *, balance=1_000_000, peer_accepts=True):
        sc = MagicMock()
        sc.internal_instance_exists.return_value = False
        sc.client_exists.return_value = True
        sc.get_delegated_token_by_id.return_value = PEER_TOKEN
        sc.get_external_father_id.return_value = FATHER_CLIENT
        sc.get_peer_id_by_external_service.return_value = PEER_ID
        sc.get_delegated_balance.return_value = balance
        sc.get_delegated_peer_balance.return_value = 2 * balance
        sc.get_client_balance.return_value = (10 ** 12, 0, 0)
        payment_system = SimpleNamespace(
            local_mu_per_unit=1_000_000_000, peer_mu_per_unit=2_000_000_000
        )
        sent = []

        def client_grpc(**kwargs):
            sent.append(kwargs["input"])
            return iter([celaut.ModifyDepositOutput(
                success=peer_accepts, message="ok" if peer_accepts else "refused"
            )])

        with patch.object(manager, "sc", sc), \
                patch.object(manager, "resolve_instance_token", return_value=None), \
                patch.object(manager, "format_mu", str), \
                patch.object(manager, "matching_payment_system", return_value=payment_system), \
                patch.object(manager, "peer_channel"), \
                patch.object(manager.celaut_pb2_grpc, "GatewayStub"), \
                patch.object(manager.bee, "client_grpc", side_effect=client_grpc):
            ok, message = manager.modify_deposit(
                amount_mu=amount_mu, service_token=OUR_ALIAS
            )
        return ok, message, sc, sent

    def test_a_top_up_lands_on_the_local_row_and_travels_in_the_peers_mu(self):
        """The father was charged here, so the row he is refunded from moves here.

        What travels is the same figure on the peer's scale, the crossing
        `configuration_for_peer` makes for the initial deposit -- sending our own
        number would fund the child at whatever the peer's rate happens to mean.
        """
        ok, _, sc, sent = self._modify(500_000)

        self.assertTrue(ok)
        self.assertEqual(
            sc.update_delegated_deposit.call_args.kwargs["balance_mu"], 1_500_000
        )
        self.assertEqual(int(sent[0].difference.n), 1_000_000)

    def test_a_top_up_moves_the_mark_the_tick_measures_against(self):
        """Or the interval it lands in reads as no consumption at all.

        The tick charges the fall in the peer's figure. A top-up raises that figure
        without the child having spent anything, so the mark has to rise with it --
        otherwise the next reading looks like a child that used nothing, and a whole
        interval of real usage is written off.
        """
        _, _, sc, _ = self._modify(500_000)

        sc.update_delegated_deposit.assert_called_once_with(
            token=PEER_TOKEN, balance_mu=1_500_000, peer_balance_mu=3_000_000
        )

    def test_a_withdrawal_moves_both_the_same_way(self):
        ok, _, sc, sent = self._modify(-400_000)

        self.assertTrue(ok)
        sc.update_delegated_deposit.assert_called_once_with(
            token=PEER_TOKEN, balance_mu=600_000, peer_balance_mu=1_200_000
        )
        self.assertEqual(int(sent[0].difference.n), -800_000)

    def test_a_peer_that_refuses_leaves_the_local_row_alone(self):
        """A row raised for a top-up the peer never took is a refund out of thin air.

        The father is handed that figure back when the instance stops, for runtime
        this node never bought -- so the local row follows the peer, not the intent.
        """
        ok, _, sc, _ = self._modify(500_000, peer_accepts=False)

        self.assertFalse(ok)
        sc.update_delegated_deposit.assert_not_called()

    def test_taking_out_more_than_is_there_is_refused(self):
        ok, message, sc, sent = self._modify(-2_000_000)

        self.assertFalse(ok)
        sc.update_delegated_deposit.assert_not_called()
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
