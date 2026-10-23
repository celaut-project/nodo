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
* the maintenance tick charges it what this node advertises for the child, as for a
  local one (#577), and measures what the peer charges this node (#570);
* the stop hands whatever is left back to the father, after the row is gone;
* the peer's own refund settles on the peer, and is only logged here.

These tests stop at the database boundary on purpose: what is under test is that the
money is moved to the right account, in the right amount, at the right point in the
sequence.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.database import ledger
    from src.gateway.launcher.delegate_execution import delegate_execution as delegate_mod
    from src.manager import maintain, manager, statement_check
    from src.utils import dead_instances
    from tests.statement_chain import FakeLedger
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
    """What `delegate_execution` sends to the peer, and what it writes on the row it creates.

    The father pays `initial_mu` and nothing else (#563). This node keeps its own build
    and sends the peer the rest, in the MU of the peer (#577). The peer takes its build
    from that amount. The peer MU are worth half of ours here (two of them for one local MU).
    """

    PAYMENT_SYSTEM = SimpleNamespace(
        local_mu_per_unit=1_000_000_000, peer_mu_per_unit=2_000_000_000
    )

    def _delegate(self, *, initial_mu, own_build_mu=0, peer_build_mu=0, child_balance=None,
                  start_error=None, mark=None, record_error=None, stop_error=None):
        """Run a delegation. ``child_balance`` is what the peer reports, in its MU, and
        ``mark`` the last movement of its statement."""
        config = celaut.Configuration()
        if initial_mu is not None:
            config.initial_mu.n = str(initial_mu)
        instance = celaut.ServiceInstance(token=PEER_TOKEN)
        refunds = []
        sent = {}

        def capture(**kwargs):
            sent["config"] = kwargs["config"]
            return MagicMock()

        with patch.object(
            delegate_mod, "matching_payment_system", return_value=self.PAYMENT_SYSTEM
        ), patch.object(
            delegate_mod, "balance_on_other_peer", return_value=10 ** 12
        ), patch.object(
            delegate_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(delegate_mod, "peer_channel"), patch.object(
            delegate_mod.utils, "service_extended", side_effect=capture
        ), patch.object(
            delegate_mod.BeeClient, "start_service", return_value=instance, side_effect=start_error
        ) as start, patch.object(
            delegate_mod.delegated_endpoints, "should_tunnel", return_value=False
        ), patch.object(
            delegate_mod, "_check_start", return_value=(child_balance, mark)
        ) as check_start, patch.object(delegate_mod, "SQLConnection") as sql_connection, patch.object(
            delegate_mod.BeeClient, "stop_service", side_effect=stop_error
        ) as stop_service:
            sql_connection.return_value.add_delegated_instance.side_effect = record_error
            error = None
            try:
                delegate_mod.delegate_execution(
                    service_id="service-a",
                    peer=PEER_ID,
                    father_id=FATHER_CLIENT,
                    metadata=celaut.Metadata(),
                    config=config,
                    recursion_guard_token="token-a",
                    refund_container=[lambda: refunds.append(True)],
                    own_build_mu=own_build_mu,
                    peer_build_mu=peer_build_mu,
                )
            except Exception as e:
                error = e
        return SimpleNamespace(
            error=error, refunds=refunds, sent=sent.get("config"), start=start,
            check_start=check_start, stop_service=stop_service,
            written=(
                sql_connection.return_value.add_delegated_instance.call_args.kwargs
                if sql_connection.return_value.add_delegated_instance.called else None
            ),
        )

    def test_the_peer_is_sent_what_the_father_paid_less_the_build_of_this_node(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           child_balance=1_400_000)

        self.assertIsNone(r.error)
        # 900 000 of ours, and two peer MU for each.
        self.assertEqual(int(r.sent.initial_mu.n), 1_800_000)

    def test_the_row_keeps_what_the_father_can_get_back_in_our_mu(self):
        """What the father paid, less the build of this node: as a local child has it.

        The peer took 400 000 of its MU for its own build: that is the cost of this node,
        and no part of what the father can get back (#577).
        """
        r = self._delegate(own_build_mu=100_000, peer_build_mu=200_000, initial_mu=1_000_000,
                           child_balance=1_400_000)

        self.assertEqual(r.written["balance_mu"], 900_000)

    def test_the_mark_of_the_tick_is_the_balance_that_the_peer_reports(self):
        """Not the amount that travelled: the peer already took its build from it.

        A mark at the amount sent would charge the build to the client again at the first
        tick, as consumption.
        """
        mark = {"seq": 2, "ts": 100, "hash": "abc"}
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           child_balance=1_400_000, mark=mark)

        self.assertEqual(r.written["peer_balance_mu"], 1_400_000)
        self.assertEqual(r.written["mark"], mark)

    def test_the_chain_of_the_new_child_is_read_for_its_mark(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           child_balance=1_400_000)

        r.check_start.assert_called_once()
        args = r.check_start.call_args.args
        self.assertEqual(args[:3], (PEER_ID, "client-on-peer", PEER_TOKEN))
        # The mark is as old as the request: the account was written after it.
        self.assertLessEqual(args[3], time.time())

    def test_the_account_opens_with_the_start_and_the_build_of_this_node(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           child_balance=1_400_000)

        movements = r.written["movements"]
        self.assertEqual([m.kind for m in movements], [ledger.START, ledger.BUILD])
        self.assertEqual([m.amount_mu for m in movements], [1_000_000, -100_000])
        # Nothing of the peer: the child is one more instance of this node.
        self.assertTrue(all(not m.detail for m in movements))
        self.assertEqual(r.written["sent_peer_mu"], 1_800_000)

    def test_an_unreadable_peer_leaves_the_balance_of_this_node_and_the_quote_of_the_peer(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           child_balance=None)

        self.assertIsNone(r.error)
        self.assertEqual(r.written["balance_mu"], 900_000)
        # What was sent, less the build that the peer quoted: 1 800 000 - 400 000.
        self.assertEqual(r.written["peer_balance_mu"], 1_400_000)

    def test_the_balance_of_the_row_does_not_follow_what_the_peer_reports(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=0,
                           child_balance=9_000_000)

        self.assertEqual(r.written["balance_mu"], 900_000)
        self.assertEqual(r.written["peer_balance_mu"], 9_000_000)

    def test_an_amount_that_the_build_of_this_node_takes_whole_is_refused_and_refunded(self):
        r = self._delegate(initial_mu=100_000, own_build_mu=100_000)

        self.assertIsNotNone(r.error)
        self.assertEqual(r.refunds, [True])
        r.start.assert_not_called()

    def test_a_start_that_the_peer_rejects_refunds_the_father(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           start_error=Exception("initial_mu is less than the build"))

        self.assertIsNotNone(r.error)
        self.assertEqual(r.refunds, [True])
        self.assertIsNone(r.written)

    def test_a_start_that_fails_after_the_peer_answered_stops_the_child_there(self):
        """The father got his MU back and an error: nobody holds the instance, and left
        alone it would run on the MU that this node sent until they ran out."""
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           child_balance=1_400_000, record_error=Exception("disk is full"))

        self.assertIsNotNone(r.error)
        self.assertEqual(r.refunds, [True])
        r.stop_service.assert_called_once()
        self.assertEqual(r.stop_service.call_args.kwargs["token"], PEER_TOKEN)

    def test_a_cleanup_that_fails_does_not_hide_the_error_or_the_refund(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           child_balance=1_400_000, record_error=Exception("disk is full"),
                           stop_error=Exception("peer is gone"))

        self.assertEqual(str(r.error), "disk is full")
        self.assertEqual(r.refunds, [True])

    def test_a_start_that_never_got_a_token_has_nothing_to_stop(self):
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=200_000,
                           start_error=Exception("timed out"))

        self.assertIsNotNone(r.error)
        r.stop_service.assert_not_called()

    def test_a_child_that_already_spent_keeps_the_instance(self):
        """The child reads less than the amount less the build: a tick, or its dependencies.

        Two readings of a balance cannot tell that from a peer that took too much, so the
        start goes on. What the child is charged is this node's price, from the first tick.
        """
        r = self._delegate(initial_mu=1_000_000, own_build_mu=100_000, peer_build_mu=0,
                           child_balance=1_000_000)

        self.assertIsNone(r.error)
        self.assertEqual(r.written["balance_mu"], 900_000)
        self.assertEqual(r.written["peer_balance_mu"], 1_000_000)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedMaintenanceTickTests(unittest.TestCase):
    """The sweep that charges a delegated deposit what this node advertises (#577)."""

    MARK = {"peer_balance_mu": 1_400_000, "seq": 9, "ts": 190, "hash": "abc"}
    OWN_MU = 250_000

    def _row(self, **overrides):
        row = {
            'token': PEER_TOKEN,
            'id': OUR_ALIAS,
            'peer_id': PEER_ID,
            'father_id': FATHER_CLIENT,
            'serialized_instance': None,
            'balance_mu': 1_000_000,
            'peer_balance_mu': 2_000_000,
            'mem_limit': 1 << 30, 'disk_space': 1 << 30, 'cpu_period': 100_000,
            'cpu_quota': 100_000, 'arch': None, 'charged_until': time.time() - 60,
        }
        row.update(overrides)
        return row

    def _tick(self, row, *, spent=600_000, read_raises=None, peer_reachable=True,
              rows=None, due=True, own_mu=OWN_MU):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = rows or [row]
        sc.get_delegated_row.side_effect = lambda token: next(
            (r for r in (rows or [row]) if r['token'] == token), None)
        sc.statement_tick_due.return_value = due
        read = (
            MagicMock(side_effect=read_raises) if read_raises
            else MagicMock(return_value=(spent, self.MARK))
        )
        with patch.object(maintain, "sc", sc), \
                patch.object(maintain, "_check_our_account") as check_account, \
                patch.object(maintain, "_read_child", read), \
                patch.object(maintain, "is_peer_available", return_value=peer_reachable), \
                patch.object(maintain, "system_scarcity", return_value={}), \
                patch.object(maintain, "compute_maintenance_cost", return_value=own_mu), \
                patch.object(maintain, "stop_instance") as stop, \
                patch.object(maintain, "ALLOW_DEBT", False), \
                patch.object(maintain, "format_mu", str):
            maintain.maintain_delegated_instances()
        self.check_account = check_account
        return sc, stop

    def _mark(self, sc):
        """The mark that was written, less the time that it is charged up to."""
        mark = dict(sc.settle_delegated_tick.call_args.kwargs["mark"])
        self.assertIn("charged_until", mark)
        del mark["charged_until"]
        return mark

    def test_our_account_is_read_only_when_its_ticks_are_due(self):
        self._tick(self._row(), due=False)

        self.check_account.assert_not_called()

    def test_our_account_on_each_peer_is_checked_once(self):
        self._tick(None, rows=[self._row(), self._row(token="second-token")])

        self.check_account.assert_called_once_with(PEER_ID, debug_mode=False)

    def test_the_client_pays_the_price_of_this_node_and_not_what_the_peer_metered(self):
        """The peer metered 600 000 of its MU. The child is charged 250 000, our price."""
        sc, stop = self._tick(self._row(), spent=600_000)

        kwargs = sc.settle_delegated_tick.call_args.kwargs
        self.assertEqual(kwargs["charge_mu"], self.OWN_MU)
        self.assertEqual(self._mark(sc), self.MARK)
        # The row says what a local one says, and nothing of the peer.
        self.assertEqual(set(kwargs["detail"]),
                         {"seconds", "mem_limit", "disk_space", "cpu_period", "cpu_quota"})
        stop.assert_not_called()

    def test_an_instance_that_cannot_pay_its_price_is_stopped(self):
        """The same reaper a local instance gets, and the row is emptied first.

        Every MU on it is already owed for runtime that happened, so letting
        `stop_instance` refund it would hand the father back time this node paid for.
        """
        sc, stop = self._tick(self._row(balance_mu=100), spent=600_000)

        stop.assert_called_once_with(token=OUR_ALIAS, reason=dead_instances.OUT_OF_BALANCE)
        self.assertEqual(sc.settle_delegated_tick.call_args.kwargs["charge_mu"], 100)

    def test_a_peer_that_cannot_be_reached_charges_nothing_and_loses_nothing(self):
        """The mark stays put, and so does the time that was charged up to, so the next
        reading that works charges the whole gap.

        A tick that charged a guess here, or advanced the mark without charging,
        would turn an unreachable peer into free runtime or into an invented bill.
        """
        sc, stop = self._tick(
            self._row(), read_raises=RuntimeError("unreachable"), peer_reachable=False
        )

        sc.settle_delegated_tick.assert_not_called()
        stop.assert_not_called()

    def test_a_peer_that_no_longer_knows_the_instance_stops_it_here_too(self):
        """It is up and answering; the instance is simply gone there.

        Left alone, the row would keep a father's deposit hostage to an instance
        that no longer runs anywhere.
        """
        sc, stop = self._tick(
            self._row(), read_raises=RuntimeError("unknown token"), peer_reachable=True
        )

        stop.assert_called_once_with(token=OUR_ALIAS, reason=dead_instances.PEER_STOPPED)

    def test_a_child_with_nothing_to_charge_is_only_re_marked(self):
        sc, stop = self._tick(self._row(), own_mu=0)

        kwargs = sc.settle_delegated_tick.call_args.kwargs
        self.assertEqual(kwargs["charge_mu"], 0)
        self.assertEqual(self._mark(sc), self.MARK)
        stop.assert_not_called()

    def test_one_instance_that_will_not_stop_does_not_strand_the_rest(self):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = [
            self._row(id="wont-stop", balance_mu=0),
            self._row(token="second-token"),
        ]
        sc.get_delegated_row.side_effect = lambda token: next(
            r for r in sc.get_delegated_instances.return_value if r['token'] == token)
        with patch.object(maintain, "sc", sc), \
                patch.object(maintain, "_check_our_account"), \
                patch.object(maintain, "_read_child", return_value=(600_000, self.MARK)), \
                patch.object(maintain, "is_peer_available", return_value=True), \
                patch.object(maintain, "system_scarcity", return_value={}), \
                patch.object(maintain, "compute_maintenance_cost", return_value=self.OWN_MU), \
                patch.object(maintain, "stop_instance", side_effect=[RuntimeError("no"), None]), \
                patch.object(maintain, "ALLOW_DEBT", False), \
                patch.object(maintain, "format_mu", str):
            maintain.maintain_delegated_instances()

        self.assertEqual(sc.settle_delegated_tick.call_args.kwargs["token"], "second-token")
        self.assertEqual(sc.settle_delegated_tick.call_args.kwargs["charge_mu"], self.OWN_MU)


def _book(*movements, account=PEER_TOKEN):
    """A ledger from (amount, kind[, ref, detail, ts]), chained as a node chains it."""
    book = FakeLedger(account)
    for movement in movements:
        book.add(movement[0], movement[1], *movement[2:5])
    return book


def _answer(book, from_seq, tamper=None):
    """What a node answers for ``from_seq``, dated now, optionally changed by ``tamper``."""
    statement = book.statement(from_seq=from_seq, ts=int(time.time()))
    if tamper:
        tamper(statement)
    return statement


def _answer_page(book, from_seq, rows):
    """What a node answers for ``rows`` movements from ``from_seq``, dated now."""
    return book.statement(from_seq=from_seq, to_seq=from_seq + rows - 1 if rows else 0,
                          ts=int(time.time()))


def _fetch_of(book, tamper=None):
    return MagicMock(side_effect=lambda peer, client, token="", from_seq=0, rows=None:
                     _answer(book, from_seq, tamper))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReadChildTests(unittest.TestCase):
    """What a child owes at a tick: what its statements show, and what its balance fell by
    since the last of them once what its father moved is taken out."""

    def _row(self, book, seq=2, balance=70, verified=0, verified_sent=0, sent=0, charged=0):
        mark = book.mark_at(seq)
        return {'token': PEER_TOKEN, 'id': OUR_ALIAS, 'peer_id': PEER_ID,
                'peer_balance_mu': balance, 'peer_statement_seq': seq,
                'peer_statement_hash': mark.hash, 'peer_statement_ts': mark.ts,
                'verified_consumption_peer_mu': verified,
                'verified_father_net_peer_mu': verified_sent,
                'father_net_peer_mu': sent, 'charged_peer_mu': charged}

    def _read(self, book=None, fetch_error=None, balance=None, row=None, tamper=None,
              held=10 ** 9, rates=None, due=True, force=False):
        fetch = MagicMock(side_effect=fetch_error) if fetch_error else _fetch_of(book, tamper)
        sc = MagicMock()
        sc.child_statement_due.return_value = due
        with patch.object(maintain, "sc", sc), \
                patch.object(statement_check, "read", fetch), \
                patch.object(maintain, "_balance_held_on", return_value=held), \
                patch.object(statement_check, "stored_rates", return_value=rates or {}), \
                patch.object(maintain, "get_client_id_on_other_peer", return_value="client-on-peer"), \
                patch.object(maintain, "_peer_rates", return_value={}), \
                patch.object(maintain, "instance_balance_on_peer",
                             side_effect=RuntimeError("gone") if balance is None else None,
                             return_value=balance), \
                patch.object(statement_check, "settle_failed_reads"), \
                patch.object(statement_check, "penalise_once") as penalise, \
                patch.object(statement_check, "penalise_no_statement") as self.penalise_none:
            try:
                result = maintain._read_child(dict(row), force_statement=force)
            except Exception as e:
                result = e
        self.sc = sc
        return result, penalise, fetch

    def test_the_child_owes_what_its_statement_says_since_the_mark(self):
        book = _book((100, "start"), (-30, "build"), (-5, "maintenance"), (50, "top_up"),
                     (-7, "child_start"))

        (charge, state), penalise, fetch = self._read(book, row=self._row(book, sent=50))

        # The top-up is the father's, not consumption.
        self.assertEqual(charge, 12)
        self.assertEqual((state["peer_balance_mu"], state["seq"], state["hash"]),
                         (108, 5, book.rows[4]["hash"]))
        self.assertEqual((state["verified_consumption"], state["verified_father_net"],
                          state["charged_delta"]), (12, 50, 12))
        penalise.assert_not_called()
        self.assertEqual(fetch.call_args.kwargs["from_seq"], 3)

    def test_between_statements_the_balance_says_what_was_consumed(self):
        book = _book((100, "start"), (-30, "build"))

        (charge, state), penalise, fetch = self._read(
            book, row=self._row(book), balance=60, due=False)

        fetch.assert_not_called()
        self.assertEqual(charge, 10)
        # The mark does not move: no statement was read.
        self.assertEqual((state["seq"], state["hash"], state["peer_balance_mu"]),
                         (2, book.rows[1]["hash"], 70))
        self.assertEqual(state["charged_delta"], 10)

    def test_a_withdrawal_of_the_father_is_not_consumption(self):
        """The balance fell by 100 because the father took them out: nothing was spent."""
        book = _book((1000, "start"), (0, "build"))

        (charge, _), _, _ = self._read(
            book, row=self._row(book, balance=1000, sent=-100), balance=900, due=False)

        self.assertEqual(charge, 0)

    def test_a_top_up_of_the_father_hides_no_consumption(self):
        """The balance rose by 150 with 200 sent: 50 were spent."""
        book = _book((1000, "start"), (0, "build"))

        (charge, _), _, _ = self._read(
            book, row=self._row(book, balance=1000, sent=200), balance=1150, due=False)

        self.assertEqual(charge, 50)

    def test_a_statement_corrects_what_the_balance_overcharged(self):
        book = _book((100, "start"), (-30, "build"), (-8, "maintenance"))

        (charge, state), _, _ = self._read(
            book, row=self._row(book, charged=10))

        # The statement says 8 and 10 were charged: 2 go back.
        self.assertEqual(charge, -2)
        self.assertEqual(state["charged_delta"], -2)

    def test_a_statement_corrects_what_the_balance_undercharged(self):
        book = _book((100, "start"), (-30, "build"), (-15, "maintenance"))

        (charge, _), _, _ = self._read(book, row=self._row(book, charged=10))

        self.assertEqual(charge, 5)

    def test_what_was_charged_already_is_not_charged_twice(self):
        book = _book((100, "start"), (-30, "build"))

        (charge, _), _, _ = self._read(
            book, row=self._row(book, verified=10, charged=10), balance=70, due=False)

        self.assertEqual(charge, 0)

    def test_a_statement_that_does_not_add_up_is_scored_and_the_balance_takes_its_place(self):
        book = _book((100, "start"), (-30, "build"), (-5, "maintenance"))

        def tamper(statement):
            statement.balance.n = "60"

        (charge, state), penalise, _ = self._read(book, row=self._row(book), tamper=tamper)

        self.assertEqual(penalise.call_args.args[2], "statement_inconsistent")
        # 70 at the mark, 60 at the head: 10, and the mark goes to the head.
        self.assertEqual(charge, 10)
        self.assertEqual((state["peer_balance_mu"], state["seq"]), (60, 3))

    def test_what_cannot_be_checked_never_gives_MU_back(self):
        book = _book((100, "start"), (-30, "build"), (20, "refund"))

        def tamper(statement):
            statement.balance.n = "999"

        (charge, _), _, _ = self._read(book, row=self._row(book, charged=0), tamper=tamper)

        self.assertEqual(charge, 0)

    def test_a_gap_that_the_retention_explains_is_not_a_fault_and_goes_to_the_head(self):
        book = _book((100, "start"), (-30, "build"), (-5, "maintenance"), (-5, "maintenance"))
        row = self._row(book, seq=1, balance=100)
        row["peer_statement_ts"] = 0
        book.purge_before(3)

        (charge, state), penalise, _ = self._read(book, row=row)

        penalise.assert_not_called()
        self.assertEqual((charge, state["seq"]), (40, 4))

    def test_a_child_with_no_mark_takes_the_head_as_its_mark(self):
        book = _book((100, "start"), (-30, "build"), (-5, "maintenance"))
        row = dict(self._row(book), peer_statement_seq=None, peer_statement_hash=None,
                   peer_statement_ts=None)

        (charge, state), penalise, fetch = self._read(book, row=row)

        self.assertEqual(charge, 5)
        self.assertEqual((state["seq"], state["hash"]), (3, book.rows[2]["hash"]))
        self.assertEqual(fetch.call_args.kwargs["from_seq"], 1)
        penalise.assert_not_called()

    def test_a_peer_that_knows_the_child_and_gives_no_statement_is_scored_and_the_balance_charges(self):
        book = _book((100, "start"), (-30, "build"))

        (charge, state), penalise, _ = self._read(
            fetch_error=RuntimeError("refused"), balance=50, row=self._row(book))

        self.penalise_none.assert_called_once()
        self.assertEqual(self.penalise_none.call_args.kwargs["seq"], 3)
        self.assertEqual(charge, 20)
        self.assertEqual(state["seq"], 2)

    def test_a_child_the_peer_does_not_know_raises_for_the_tick_to_stop_it(self):
        book = _book((100, "start"), (-30, "build"))

        result, penalise, _ = self._read(
            fetch_error=RuntimeError("no such child"), row=self._row(book))

        self.assertIsInstance(result, RuntimeError)
        penalise.assert_not_called()

    def test_a_statement_that_our_balance_does_not_pay_for_is_not_asked_for_and_no_one_is_scored(self):
        book = _book((100, "start"), (-30, "build"))

        (charge, _), penalise, fetch = self._read(
            book, balance=50, row=self._row(book), held=5,
            rates={"statement_query_mu": 100, "statement_row_mu": 10})

        fetch.assert_not_called()
        self.penalise_none.assert_not_called()
        penalise.assert_not_called()
        self.assertEqual(charge, 20)

    def test_the_read_asks_for_what_the_balance_pays_for(self):
        book = _book((100, "start"), (-30, "build"))

        _, _, fetch = self._read(book, row=self._row(book))

        self.assertEqual(fetch.call_args.kwargs["rows"], ledger.max_statement_rows())

    def test_the_statement_is_read_only_when_it_is_due_or_asked_for(self):
        book = _book((100, "start"), (-30, "build"))

        _, _, fetch = self._read(book, row=self._row(book), balance=70, due=False)
        fetch.assert_not_called()
        self.sc.child_statement_due.assert_called_once()

        _, _, fetch = self._read(book, row=self._row(book), balance=70, due=False, force=True)
        fetch.assert_called_once()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SettleChildTests(unittest.TestCase):
    """What the tick does with a child: charge our price, or stop it (#577)."""

    ROW = {
        'token': PEER_TOKEN, 'id': OUR_ALIAS, 'peer_id': PEER_ID, 'balance_mu': 1_000_000,
        'mem_limit': 1 << 30, 'disk_space': 2 << 30, 'cpu_period': 100_000, 'cpu_quota': 50_000,
        'arch': "linux/amd64", 'launch_cpu_bp': 10_000, 'launch_mem_bp': 11_000,
        'launch_disk_bp': 12_000, 'charged_until': 1000.0,
    }
    STATE = {"peer_balance_mu": 1, "seq": 2, "hash": "h", "ts": 3}

    def _settle(self, own_mu, charge_peer_mu=0, row=None, now=1060.0, **kwargs):
        sc = MagicMock()
        with patch.object(maintain, "sc", sc), \
                patch.object(maintain.time, "time", return_value=now), \
                patch.object(maintain, "system_scarcity", return_value={"cpu": 1.0}), \
                patch.object(maintain, "compute_maintenance_cost", return_value=own_mu) as cost, \
                patch.object(maintain, "stop_instance") as stop, \
                patch.object(maintain, "ALLOW_DEBT", False), \
                patch.object(maintain, "format_mu", str):
            result = maintain.settle_child(
                row or dict(self.ROW), charge_peer_mu, dict(self.STATE), **kwargs)
        return result, sc, stop, cost

    def test_the_charge_is_the_cost_function_of_a_local_instance_on_the_shape_of_the_row(self):
        ok, sc, _, cost = self._settle(222)

        self.assertTrue(ok)
        kwargs = cost.call_args.kwargs
        resources = kwargs["system_resources"]
        self.assertEqual((resources.mem_limit, resources.disk_space,
                          resources.cpu_period, resources.cpu_quota),
                         (1 << 30, 2 << 30, 100_000, 50_000))
        # The time since the last charge, our scarcity, the arch and the price at launch.
        self.assertEqual(kwargs["seconds"], 60.0)
        self.assertEqual(kwargs["scarcity"], {"cpu": 1.0})
        self.assertEqual(kwargs["arch"], "linux/amd64")
        self.assertEqual(kwargs["launch_bp"], {"cpu": 10_000, "mem": 11_000, "disk": 12_000})
        self.assertEqual(sc.settle_delegated_tick.call_args.kwargs["charge_mu"], 222)

    def test_the_time_charged_up_to_moves_with_the_charge(self):
        _, sc, _, _ = self._settle(222)

        self.assertEqual(sc.settle_delegated_tick.call_args.kwargs["mark"],
                         dict(self.STATE, charged_until=1060.0))

    def test_what_the_peer_charged_is_no_part_of_the_charge(self):
        # The peer metered more, and a correction that gives back, and neither reaches it.
        for charge_peer_mu in (10 ** 6, -401):
            _, sc, _, _ = self._settle(222, charge_peer_mu=charge_peer_mu)
            kwargs = sc.settle_delegated_tick.call_args.kwargs
            self.assertEqual(kwargs["charge_mu"], 222)
            self.assertNotIn("refund_mu", kwargs)

    def test_nothing_to_charge_only_moves_the_state(self):
        ok, sc, _, _ = self._settle(0)

        self.assertTrue(ok)
        sc.settle_delegated_tick.assert_called_once_with(
            token=PEER_TOKEN, charge_mu=0, mark=dict(self.STATE, charged_until=1060.0))

    def test_a_deposit_that_does_not_cover_the_charge_is_emptied_and_the_instance_stopped(self):
        ok, sc, stop, _ = self._settle(10 ** 9)

        self.assertTrue(ok)
        self.assertEqual(sc.settle_delegated_tick.call_args.kwargs["charge_mu"], 1_000_000)
        stop.assert_called_once_with(token=OUR_ALIAS, reason=dead_instances.OUT_OF_BALANCE)

    def test_a_stop_that_is_under_way_charges_the_whole_figure_and_stops_nothing(self):
        ok, sc, stop, _ = self._settle(10 ** 9, stop_if_short=False)

        self.assertGreater(sc.settle_delegated_tick.call_args.kwargs["charge_mu"], 1_000_000)
        stop.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloseDelegatedChargeTests(unittest.TestCase):
    """Right before a delegated child is stopped its statement is read one last time."""

    ROW = {'token': PEER_TOKEN, 'id': OUR_ALIAS, 'peer_id': PEER_ID}

    def test_the_charge_is_closed_from_a_statement_that_is_asked_for(self):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = [dict(self.ROW)]
        with patch.object(maintain, "sc", sc), \
                patch.object(maintain, "_read_child", return_value=(7, {"peer_balance_mu": 1})) as read, \
                patch.object(maintain, "settle_child") as settle:
            maintain.close_delegated_charge(PEER_TOKEN)

        self.assertTrue(read.call_args.kwargs["force_statement"])
        self.assertEqual(settle.call_args.args[1], 7)
        self.assertFalse(settle.call_args.kwargs["stop_if_short"])

    def test_a_failure_to_close_never_stops_the_stop(self):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = [dict(self.ROW)]
        with patch.object(maintain, "sc", sc), \
                patch.object(maintain, "_read_child", side_effect=RuntimeError("peer is gone")), \
                patch.object(maintain, "settle_child") as settle:
            maintain.close_delegated_charge(PEER_TOKEN)

        settle.assert_not_called()

    def test_an_instance_that_is_not_there_is_ignored(self):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = []
        with patch.object(maintain, "sc", sc), patch.object(maintain, "_read_child") as read:
            maintain.close_delegated_charge(PEER_TOKEN)

        read.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CheckStartTests(unittest.TestCase):
    """The statement of a new child, read right after StartService (#570)."""

    def _check(self, book=None, fetch_error=None, tamper=None, held=10 ** 9):
        fetch = MagicMock(side_effect=fetch_error) if fetch_error else _fetch_of(book, tamper)
        with patch.object(statement_check, "read", fetch), \
                patch.object(delegate_mod, "_child_balance_on_peer", return_value=55), \
                patch.object(delegate_mod, "refresh_balance_on_peer", return_value=held), \
                patch.object(statement_check, "stored_rates", return_value=self.RATES), \
                patch.object(statement_check, "settle_failed_reads"), \
                patch.object(statement_check, "penalise_once") as penalise, \
                patch.object(statement_check, "penalise_no_statement",
                             side_effect=lambda peer, account, error, seq=0, **kw: penalise(
                                 peer, account, "statement_inconsistent", str(error), seq)):
            result = delegate_mod._check_start(
                PEER_ID, "client-on-peer", PEER_TOKEN, started_at=1000)
        self.fetch = fetch
        return result, penalise

    RATES = {"statement_query_mu": 100, "statement_row_mu": 10}

    def test_the_read_asks_for_what_the_balance_pays_for(self):
        self._check(_book((100, "start"), (-30, "build")), held=150)

        # (150 - 100) // 10 - 1 rows.
        self.assertEqual(self.fetch.call_args.kwargs["rows"], 4)

    def test_a_balance_that_pays_for_nothing_reads_no_statement_and_scores_nothing(self):
        (balance, mark), penalise = self._check(_book((100, "start")), held=99)

        self.fetch.assert_not_called()
        penalise.assert_not_called()
        self.assertEqual((balance, mark), (55, None))

    def test_a_start_marks_its_last_movement(self):
        book = _book((100, "start"), (-30, "build"))

        (balance, mark), penalise = self._check(book)

        self.assertEqual(balance, 70)
        self.assertEqual((mark["seq"], mark["hash"]), (2, book.rows[1]["hash"]))
        penalise.assert_not_called()

    def test_what_the_peer_took_for_the_build_is_not_judged(self):
        (balance, mark), penalise = self._check(_book((100, "start"), (-90, "build")))

        self.assertEqual((balance, mark["seq"]), (10, 2))
        penalise.assert_not_called()

    def test_a_statement_that_does_not_start_at_its_first_movement_does_not_add_up(self):
        def tamper(statement):
            statement.movements.pop(0)

        (balance, mark), penalise = self._check(
            _book((100, "start"), (-30, "build")), tamper=tamper)

        self.assertEqual(penalise.call_args.args[2], "statement_inconsistent")
        self.assertEqual((balance, mark), (55, None))

    def test_a_start_that_hangs_from_another_account_does_not_add_up(self):
        (balance, mark), penalise = self._check(
            _book((100, "start"), (-30, "build"), account="another-account"))

        self.assertEqual(penalise.call_args.args[2], "statement_inconsistent")

    def test_no_statement_from_the_peer_that_just_started_the_child(self):
        (balance, mark), penalise = self._check(fetch_error=RuntimeError("refused"))

        self.assertEqual(penalise.call_args.args[2], "statement_inconsistent")
        self.assertEqual((balance, mark), (55, None))


# A start of this node (#578): its reference, and the detail of its ``child_start``. The
# check takes it for one of ours (``_check``).
_REF = "ab" * 32
_START = '{"start_ref":"%s"}' % _REF


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CheckOurAccountTests(unittest.TestCase):
    """The statement of our own account on a peer, read at each tick (#570)."""

    # What the books hold before the page is read: the charges of the page are added to
    # them with the mark.
    COUNTERS = {"reads": 10, "rows_priced": 100, "verified": 0, "verified_mu": 0}
    RATES = {"statement_query_mu": 0, "statement_row_mu": 1}

    def _check(self, book, stored=None, starts=None, held=10 ** 9, rates=None, counters=None,
               fetch=None, max_pages=None, attempts=None):
        sc = MagicMock()
        sc.get_peer_client.return_value = "client-on-peer"
        sc.get_peer_statement_mark.return_value = stored or {}
        sc.delegated_starts_on_peer.return_value = starts or {ledger.instance_ref(PEER_TOKEN): 100}
        sc.start_attempts.return_value = attempts or []
        sc.orphan_starts.return_value = []
        sc.top_up_reservations.return_value = [(1, ledger.instance_ref(PEER_TOKEN), 1000, 1000)]
        sc.get_statement_counters.return_value = counters or dict(self.COUNTERS)
        fetch = fetch or _fetch_of(book)
        with patch.object(maintain, "sc", sc), \
                patch.object(statement_check, "read", fetch), \
                patch.object(maintain, "_balance_held_on", return_value=held), \
                patch.object(statement_check, "stored_rates", return_value=rates or self.RATES), \
                patch.object(maintain, "_peer_rates", return_value=rates or self.RATES), \
                patch.object(statement_check, "settle_failed_reads"), \
                patch.object(statement_check.start_refs, "ours", side_effect=lambda r: r == _REF), \
                patch.object(statement_check, "penalise_once") as penalise, \
                patch.object(statement_check, "penalise_no_statement") as penalise_none:
            if max_pages:
                with patch.object(ledger, "max_statement_pages_per_tick", return_value=max_pages):
                    maintain._check_our_account(PEER_ID)
            else:
                maintain._check_our_account(PEER_ID)
        self.penalise_none = penalise_none
        self.fetch = fetch
        return sc, penalise

    @staticmethod
    def _stored(mark):
        return {"seq": mark.seq, "hash": mark.hash, "balance_mu": mark.balance, "ts": mark.ts}

    def test_our_own_movements_move_the_mark_to_the_last_one(self):
        book = _book((500, "credit"), (-100, "child_start", "", _START),
                     (0, "child_bound", ledger.instance_ref(PEER_TOKEN), '{"starts_seq":"2"}'),
                     (-1, "statement_query"), account="client-on-peer")

        sc, penalise = self._check(book)

        penalise.assert_not_called()
        peer, seq, hash_, balance, _ = sc.set_peer_statement_mark.call_args.args
        self.assertEqual((peer, seq, hash_, balance), (PEER_ID, 4, book.rows[3]["hash"], 399))

    def test_a_charge_we_did_not_cause_is_an_overcharge_at_its_seq(self):
        book = _book((500, "credit"), (-100, "debit"), account="client-on-peer")

        _, penalise = self._check(book)

        self.assertEqual(penalise.call_args.args[2], "statement_overcharged")
        self.assertEqual(penalise.call_args.args[4], 2)

    def test_a_start_that_nobody_asked_for_is_scored_when_it_shows(self):
        now = int(time.time())
        book = _book((500, "credit", "", "", now - 5), (-100, "child_start", "", "", now),
                     account="client-on-peer")

        sc, penalise = self._check(book)

        self.assertEqual(penalise.call_args.args[2], "statement_overcharged")
        self.assertEqual(penalise.call_args.args[4], 2)

    def test_a_start_in_progress_is_matched_with_its_attempt_and_the_mark_goes_past_it(self):
        now = int(time.time())
        book = _book((500, "credit", "", "", now - 5), (-100, "child_start", "", _START, now),
                     (-999, "debit", "", "", now), account="client-on-peer")

        sc, penalise = self._check(book, attempts=[(4, _REF, now - 3, 100)])

        self.assertEqual(penalise.call_args.args[4], 3)
        self.assertEqual(sc.set_peer_statement_mark.call_args.args[1], 3)
        self.assertEqual(sc.set_peer_statement_mark.call_args.kwargs["attempts"],
                         {_REF: (2, 100, False, "")})

    def test_the_statements_queries_are_counted_once_and_the_mark_goes_past_them(self):
        now = int(time.time())
        book = _book((500, "credit", "", "", now - 5), (-100, "child_start", "", _START, now),
                     (-1, "statement_query", "", "", now), account="client-on-peer")

        sc, _ = self._check(book, attempts=[(4, _REF, now - 3, 100)])

        # With the mark, in the same transaction.
        self.assertEqual(sc.set_peer_statement_mark.call_args.kwargs["queries_verified"], (1, 1))
        self.assertEqual(sc.set_peer_statement_mark.call_args.args[1], 3)

    def test_the_top_ups_taken_are_saved_with_the_mark(self):
        ref = ledger.instance_ref(PEER_TOKEN)
        book = _book((500, "credit"), (-50, "top_up", ref), account="client-on-peer")

        sc, penalise = self._check(book)

        penalise.assert_not_called()
        self.assertEqual(sc.set_peer_statement_mark.call_args.kwargs["top_ups_taken"], {1: 50})

    def test_a_statement_that_does_not_add_up_from_the_mark_is_penalised_and_the_mark_goes_to_the_head(self):
        book = _book((500, "credit"), (-100, "statement_query"), account="client-on-peer")
        mark = book.mark_at(1)
        stored = dict(self._stored(mark), balance_mu=499)

        sc, penalise = self._check(book, stored=stored)

        self.assertEqual(penalise.call_args.args[2], "statement_inconsistent")
        self.assertEqual(penalise.call_args.args[4], 2)
        sc.set_peer_statement_mark.assert_called_once()
        self.assertEqual(sc.set_peer_statement_mark.call_args.args[1], 2)

    def test_a_statement_that_is_not_given_is_scored_at_the_movement_that_was_missing(self):
        book = _book((500, "credit"), (-1, "statement_query"), account="client-on-peer")
        mark = book.mark_at(1)

        sc, _ = self._check(book, stored=self._stored(mark),
                            fetch=MagicMock(side_effect=RuntimeError("refused")))

        self.penalise_none.assert_called_once()
        self.assertEqual(self.penalise_none.call_args.kwargs["seq"], 2)
        sc.set_peer_statement_mark.assert_not_called()

    def test_a_read_asks_for_what_the_balance_pays_for(self):
        book = _book(*[(1, "deposit")] * 30, account="client-on-peer")

        self._check(book, held=100 + 2 * 10, rates={"statement_query_mu": 100,
                                                    "statement_row_mu": 10}, max_pages=1)

        # 20 MU after the fixed part pay for two rows, and one of them is the charge.
        self.assertEqual(self.fetch.call_args.kwargs["rows"], 1)

    def test_a_balance_that_pays_for_nothing_asks_for_nothing(self):
        book = _book((500, "credit"), account="client-on-peer")

        sc, penalise = self._check(book, held=50, rates={"statement_query_mu": 100,
                                                         "statement_row_mu": 10})

        self.fetch.assert_not_called()
        penalise.assert_not_called()
        sc.set_peer_statement_mark.assert_not_called()

    RATES_100 = {"statement_query_mu": 100, "statement_row_mu": 100}

    def test_a_balance_that_metrics_gives_too_low_is_checked_against_the_signed_head(self):
        """The free ``Metrics`` figure pays for no row; the head is signed, and says that
        the account holds more: the statement is read with the balance of the head."""
        book = _book((500, "credit"), (-1, "statement_query"), account="client-on-peer")

        sc, penalise = self._check(book, held=150, rates=self.RATES_100)

        self.assertEqual([c.kwargs["from_seq"] for c in self.fetch.call_args_list], [0, 1])
        penalise.assert_not_called()
        self.assertEqual(sc.set_peer_statement_mark.call_args.args[1], 2)

    def test_a_signed_head_that_cannot_pay_either_reads_no_movement(self):
        book = _book((150, "credit"), account="client-on-peer")

        sc, penalise = self._check(book, held=150, rates=self.RATES_100)

        self.assertEqual([c.kwargs["from_seq"] for c in self.fetch.call_args_list], [0])
        sc.set_peer_statement_mark.assert_not_called()

    def test_a_head_that_is_not_the_one_of_the_mark_is_scored(self):
        book = _book((500, "credit"), (-1, "statement_query"), account="client-on-peer")
        stored = dict(self._stored(book.mark_at(2)), hash="another-hash")

        _, penalise = self._check(book, stored=stored, held=150, rates=self.RATES_100)

        self.assertEqual(penalise.call_args.args[2], "statement_inconsistent")

    def test_a_long_account_is_read_page_by_page_without_skipping_a_movement(self):
        book = _book((5, "credit"), *[(0, "deposit")] * 11, account="client-on-peer")
        fetched = []

        def fetch(peer, client, token="", from_seq=0, rows=None):
            fetched.append(from_seq)
            return _answer_page(book, from_seq, rows)

        sc, penalise = self._check(book, fetch=MagicMock(side_effect=fetch), max_pages=10,
                                   rates={"statement_query_mu": 0, "statement_row_mu": 1},
                                   held=5)

        # Four movements fit in 5 MU of rows, with one more for the charge: 4, 4, 4.
        self.assertEqual(fetched, [1, 5, 9])
        self.assertEqual(sc.set_peer_statement_mark.call_args.args[1], 12)
        penalise.assert_not_called()

    def test_a_tick_reads_no_more_than_its_pages(self):
        book = _book((5, "credit"), *[(0, "deposit")] * 11, account="client-on-peer")

        def fetch(peer, client, token="", from_seq=0, rows=None):
            return _answer_page(book, from_seq, rows)

        sc, _ = self._check(book, fetch=MagicMock(side_effect=fetch), max_pages=2,
                            rates={"statement_query_mu": 0, "statement_row_mu": 1}, held=5)

        self.assertEqual(sc.set_peer_statement_mark.call_args.args[1], 8)

    def test_the_peer_cannot_charge_more_statements_than_we_asked_for(self):
        book = _book((500, "credit"), (-1, "statement_query"), (-1, "statement_query"),
                     account="client-on-peer")

        _, penalise = self._check(
            book, counters={"reads": 1, "rows_priced": 10, "verified": 0, "verified_mu": 0})

        self.assertEqual(penalise.call_args.args[2], "statement_overcharged")
        self.assertIn("statements were asked for", penalise.call_args.args[3])

    def test_the_peer_cannot_charge_more_for_them_than_their_price(self):
        book = _book((500, "credit"), (-90, "statement_query"), account="client-on-peer")

        _, penalise = self._check(
            book, rates={"statement_query_mu": 10, "statement_row_mu": 1},
            counters={"reads": 1, "rows_priced": 5, "verified": 0, "verified_mu": 0})

        self.assertEqual(penalise.call_args.args[2], "statement_overcharged")

    def test_charges_within_what_we_asked_for_are_fine(self):
        book = _book((500, "credit"), (-14, "statement_query"), account="client-on-peer")

        _, penalise = self._check(
            book, rates={"statement_query_mu": 10, "statement_row_mu": 1},
            counters={"reads": 1, "rows_priced": 5, "verified": 0, "verified_mu": 0})

        penalise.assert_not_called()


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
        self.sc.get_external_father_id.return_value = father_id
        self.sc.get_delegated_instance.return_value = None
        self.sc.get_instance_balance.return_value = 0
        self.sc.settle_stop.side_effect = self._settle
        self.leftover = leftover
        self.purge_raises = purge_raises

        self.peer_refund = celaut.Refund(amount=celaut.Amount(n=str(peer_refund)))

    def _settle(self, account_id, reason, delegated=False, father_id=None, credit=True):
        self.calls.append(("settle", account_id, delegated, father_id, credit))
        if self.purge_raises:
            raise RuntimeError("database is locked")
        return self.leftover

    def _stop_service(self, **kwargs):
        self.calls.append(("stop_service_on_peer", PEER_TOKEN))
        if self.peer_raises:
            raise RuntimeError("peer is unreachable")
        return self.peer_refund

    def run(self, credit=True):
        with patch.object(manager, "sc", self.sc), \
                patch.object(manager, "resolve_instance_token", return_value=None), \
                patch.object(manager, "format_mu", str), \
                patch.object(manager.utils, "generate_uris_by_peer_id",
                             return_value=iter(["peer:5000"])), \
                patch.object(manager, "node_channel"), \
                patch.object(manager.BeeClient, "call_one", side_effect=self._stop_service), \
                patch.object(manager.delegated_endpoints, "close") as close:
            self.close = close
            return manager.stop_instance(token=OUR_ALIAS, credit=credit)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedStopReturnsTheDepositTests(unittest.TestCase):

    def test_the_father_gets_back_what_is_left_of_the_deposit_he_paid(self):
        harness = _StopHarness()
        refund = harness.run()

        self.assertEqual(refund, LEFTOVER)
        self.assertIn(("settle", OUR_ALIAS, True, FATHER_CLIENT, True), harness.calls)

    def test_the_peers_own_refund_is_not_the_figure_the_father_is_settled_with(self):
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
        settlements = [c for c in harness.calls if c[0] == "settle"]
        self.assertEqual(settlements, [("settle", OUR_ALIAS, True, FATHER_CLIENT, True)])

    def test_an_instance_father_is_settled_too(self):
        harness = _StopHarness(father_id="father-instance", father_is_instance=True)
        harness.run()

        self.assertIn(("settle", OUR_ALIAS, True, "father-instance", True), harness.calls)

    def test_the_settlement_waits_for_the_peer_to_confirm_the_stop(self):
        """A stop the peer does not confirm settles nothing, and is retried."""
        harness = _StopHarness()
        harness.run()

        kinds = [call[0] for call in harness.calls]
        self.assertLess(kinds.index("stop_service_on_peer"), kinds.index("settle"))

    def test_a_settlement_that_fails_returns_nothing_so_a_retry_settles_once(self):
        harness = _StopHarness(purge_raises=True)

        self.assertIsNone(harness.run())

    def test_a_peer_that_will_not_confirm_the_stop_settles_nothing(self):
        """Neither the row nor the money moves while the instance may still be running.

        The next tick tries again. Tunnel endpoints are ours alone, so those come
        down either way.
        """
        harness = _StopHarness(peer_raises=True)

        self.assertIsNone(harness.run())
        self.assertEqual([c for c in harness.calls if c[0] == "settle"], [])
        harness.close.assert_called_once_with(token=PEER_TOKEN)

    def test_a_caller_that_took_the_leftover_on_itself_is_not_second_guessed(self):
        harness = _StopHarness()
        refund = harness.run(credit=False)

        self.assertEqual(refund, LEFTOVER)
        self.assertIn(("settle", OUR_ALIAS, True, FATHER_CLIENT, False), harness.calls)

    def test_an_exhausted_child_returns_zero(self):
        harness = _StopHarness(leftover=0)

        self.assertEqual(harness.run(), 0)


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
        sc.move_balance.return_value = 7
        payment_system = SimpleNamespace(
            local_mu_per_unit=1_000_000_000, peer_mu_per_unit=2_000_000_000
        )
        sent = []

        def client_grpc(**kwargs):
            sent.append(kwargs["input"])
            return celaut.ModifyDepositOutput(
                success=peer_accepts, message="ok" if peer_accepts else "refused"
            )

        with patch.object(manager, "sc", sc), \
                patch.object(manager, "resolve_instance_token", return_value=None), \
                patch.object(manager, "format_mu", str), \
                patch.object(manager, "matching_payment_system", return_value=payment_system), \
                patch.object(manager, "peer_channel"), \
                patch.object(manager.BeeClient, "call_one", side_effect=client_grpc):
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
        self.assertEqual(sc.move_balance.call_args_list[0].args,
                         (FATHER_CLIENT, -500_000, ledger.TOP_UP))
        self.assertEqual(sc.move_balance.call_args_list[0].kwargs["instance_token"], OUR_ALIAS)
        # The rows here and the net sent to the peer (+1 000 000 of its MU) are one write.
        sc.move_delegated_deposit.assert_called_once_with(
            OUR_ALIAS, FATHER_CLIENT, 500_000, 1_000_000)
        self.assertEqual(int(sent[0].difference.n), 1_000_000)

    def test_a_top_up_does_not_move_the_mark_the_tick_measures_against(self):
        """The peer writes the top-up in the statement of the child, and the tick does
        not count it as consumption: moving the mark too would count it twice."""
        _, _, sc, _ = self._modify(500_000)

        sc.settle_delegated_tick.assert_not_called()

    def test_a_withdrawal_moves_both_the_same_way(self):
        ok, _, sc, sent = self._modify(-400_000)

        self.assertTrue(ok)
        sc.move_delegated_deposit.assert_called_once_with(
            OUR_ALIAS, FATHER_CLIENT, -400_000, -800_000)
        self.assertEqual(int(sent[0].difference.n), -800_000)

    def test_a_peer_that_refuses_leaves_the_local_row_alone(self):
        """A row raised for a top-up the peer never took is a refund out of thin air.

        The father is handed that figure back when the instance stops, for runtime
        this node never bought -- so the local row follows the peer, not the intent.
        """
        ok, _, sc, _ = self._modify(500_000, peer_accepts=False)

        self.assertFalse(ok)
        self.assertNotIn(OUR_ALIAS, [c.args[0] for c in sc.move_balance.call_args_list])
        sc.move_delegated_deposit.assert_not_called()

    def test_taking_out_more_than_is_there_is_refused(self):
        ok, message, sc, sent = self._modify(-2_000_000)

        self.assertFalse(ok)
        self.assertEqual(sent, [])

    def test_taking_out_more_than_is_there_credits_nobody(self):
        # The father used to be credited before the check, which made MU.
        _, _, sc, _ = self._modify(-2_000_000)

        sc.move_balance.assert_not_called()
        sc.transfer.assert_not_called()
        sc.move_delegated_deposit.assert_not_called()

    def test_a_withdrawal_the_peer_refuses_credits_nobody(self):
        ok, _, sc, _ = self._modify(-400_000, peer_accepts=False)

        self.assertFalse(ok)
        sc.move_balance.assert_not_called()
        sc.transfer.assert_not_called()
        sc.move_delegated_deposit.assert_not_called()

    def test_a_top_up_the_peer_refuses_goes_back_to_the_father(self):
        _, _, sc, _ = self._modify(500_000, peer_accepts=False)

        charge, refund = sc.move_balance.call_args_list
        self.assertEqual(charge.args, (FATHER_CLIENT, -500_000, ledger.TOP_UP))
        self.assertEqual(refund.args, (FATHER_CLIENT, 500_000, ledger.REFUND))
        self.assertEqual(refund.kwargs["detail"], {"refunds_seq": "7"})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LocalDepositModificationTests(unittest.TestCase):
    """`modify_deposit` on a local instance: no MU moves before the balance check."""

    def _modify(self, amount_mu, *, balance=1_000_000, token="instance-token"):
        sc = MagicMock()
        sc.internal_instance_exists.side_effect = lambda id: id == "instance-token"
        sc.get_internal_father_id.return_value = FATHER_CLIENT
        sc.get_delegated_token_by_id.return_value = None
        sc.client_exists.return_value = True
        sc.get_instance_balance.return_value = balance
        sc.get_client_balance.return_value = (10 ** 12, 0, 0)
        with patch.object(manager, "sc", sc), patch.object(manager, "format_mu", str):
            ok, message = manager.modify_deposit(amount_mu=amount_mu, service_token=token)
        return ok, message, sc

    def test_taking_out_more_than_is_there_credits_nobody(self):
        ok, _, sc = self._modify(-2_000_000)

        self.assertFalse(ok)
        sc.transfer.assert_not_called()
        sc.move_balance.assert_not_called()

    def test_a_withdrawal_moves_the_balance_to_the_father(self):
        ok, _, sc = self._modify(-400_000)

        self.assertTrue(ok)
        sc.transfer.assert_called_once_with("instance-token", FATHER_CLIENT, 400_000,
                                            ledger.WITHDRAWAL, child="instance-token")

    def test_a_top_up_moves_the_balance_from_the_father(self):
        ok, _, sc = self._modify(500_000)

        self.assertTrue(ok)
        sc.transfer.assert_called_once_with(FATHER_CLIENT, "instance-token", 500_000,
                                            ledger.TOP_UP, child="instance-token",
                                            allow_debt=manager.ALLOW_DEBT)

    def test_a_father_that_cannot_pay_tops_up_nothing(self):
        sc = MagicMock()
        sc.internal_instance_exists.side_effect = lambda id: id == "instance-token"
        sc.get_internal_father_id.return_value = FATHER_CLIENT
        sc.get_instance_balance.return_value = 0
        sc.transfer.side_effect = ledger.InsufficientBalance("no")
        with patch.object(manager, "sc", sc), patch.object(manager, "format_mu", str):
            ok, _ = manager.modify_deposit(amount_mu=500_000, service_token="instance-token")

        self.assertFalse(ok)

    def test_an_instance_name_is_not_a_token(self):
        # The RPC takes a token. A name is guessable; only `nodo modify_deposit`
        # resolves one, before it calls this.
        ok, _, sc = self._modify(500_000, token="my-instance-name")

        self.assertFalse(ok)
        sc.resolve_local_instance_reference.assert_not_called()
        sc.get_local_instance_id_by_name.assert_not_called()
        sc.move_balance.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StopByTokenOnlyTests(unittest.TestCase):
    def test_stop_instance_does_not_resolve_a_name(self):
        sc = MagicMock()
        sc.internal_instance_exists.return_value = False
        sc.get_delegated_token_by_id.return_value = None
        with patch.object(manager, "sc", sc):
            try:
                manager.stop_instance(token="my-instance-name")
            except Exception:
                pass
        sc.resolve_local_instance_reference.assert_not_called()
        sc.get_local_instance_id_by_name.assert_not_called()


if __name__ == "__main__":
    unittest.main()
