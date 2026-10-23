"""What a delegated child is charged over many ticks, against a real database (#570).

Each tick estimates from the balance of the child on the peer, and every so often a
statement replaces the estimate with what its movements say. Whatever the order and the
timing, what the child is charged in total is what it consumed: what its father moves with
``ModifyDeposit`` is not consumption, and a late statement corrects an early estimate.

The peer is a ``FakeLedger`` of the child. Its MU are ours one for one, with no
commission, so that a figure is a figure.
"""
import sqlite3
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.manager import maintain, statement_check
    from src.manager.manager import delegation_lock
    from src.utils.cost_functions import delegation_commission
    from tests.statement_chain import FakeLedger
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


PEER_TOKEN = "token-on-the-peer"
ALIAS = "alias-of-the-child"
FATHER = "father-client"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedChargeFlowTests(unittest.TestCase):

    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(self._restore)

        self.now = int(time.time())
        self.peer = FakeLedger(PEER_TOKEN, start_ts=self.now - 3600)
        self.peer.add(1000, "start")
        self.peer.add(-100, "build")
        self.sc.add_client(FATHER, 5000, None)
        self.sc.add_delegated_instance(
            father_id=FATHER, encrypted_external_token=ALIAS, external_token=PEER_TOKEN,
            peer_id="peer", serialized_instance=b"", service_id="s", balance_mu=900,
            peer_balance_mu=900, movements=(ledger.Movement(ledger.START, 900),),
            sent_peer_mu=1000, mark=self._mark(2),
        )
        self.charged = 0

    def _restore(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def _mark(self, seq):
        mark = self.peer.mark_at(seq)
        return {"seq": mark.seq, "hash": mark.hash, "ts": mark.ts}

    def consume(self, amount):
        self.peer.add(-amount, "maintenance", ts=self.now - 60 + len(self.peer.rows))

    def father_moves(self, amount):
        """The father tops the child up (positive) or takes MU out of it, as
        ``modify_deposit`` does: the peer writes it, then this node writes it with the net."""
        self.peer.add(amount, "top_up" if amount > 0 else "withdrawal",
                      ts=self.now - 60 + len(self.peer.rows))
        self.sc.move_delegated_deposit(ALIAS, FATHER, amount, amount)

    def tick(self, statement=False):
        """One maintenance tick. ``statement``: whether the statement is read at it."""
        row = self.sc.get_delegated_instances()[0]

        def answer(peer, client, token="", from_seq=0, rows=None):
            return self.peer.statement(
                from_seq=from_seq, to_seq=from_seq + rows - 1 if rows else 0, ts=self.now)

        with patch.object(maintain, "sc", self.sc), \
                patch.object(statement_check, "read", MagicMock(side_effect=answer)), \
                patch.object(maintain, "_balance_held_on", return_value=10 ** 9), \
                patch.object(statement_check, "stored_rates", return_value={}), \
                patch.object(maintain, "get_client_id_on_other_peer", return_value="client"), \
                patch.object(maintain, "instance_balance_on_peer", return_value=self.peer.balance), \
                patch.object(maintain, "peer_mu_in_local", side_effect=lambda p, a, **k: a), \
                patch.object(maintain, "stop_instance"), \
                patch.object(delegation_commission, "commission_percent", return_value=0.0), \
                patch.object(self.sc, "child_statement_due", return_value=statement):
            charge, state = maintain._read_child(row)
            maintain.settle_child(row, charge, state)
        return charge

    @property
    def deposit(self):
        return self.sc.get_delegated_balance(PEER_TOKEN)

    def test_a_statement_at_every_tick_charges_what_was_consumed(self):
        for _ in range(3):
            self.consume(5)
            self.tick(statement=True)

        self.assertEqual(self.deposit, 900 - 15)

    def test_the_balance_between_statements_charges_the_same(self):
        for _ in range(3):
            self.consume(5)
            self.tick(statement=False)
        # Estimated all along: the same as a statement would have said.
        self.assertEqual(self.deposit, 900 - 15)

        self.consume(5)
        self.tick(statement=True)

        self.assertEqual(self.deposit, 900 - 20)
        row = self.sc.get_delegated_instances()[0]
        self.assertEqual((row["verified_consumption_peer_mu"], row["charged_peer_mu"]), (20, 20))

    def test_a_withdrawal_that_lands_in_the_middle_of_a_tick_is_not_charged_as_consumption(self):
        """``modify_deposit`` holds the lock of the deposit from the call to the peer until
        the net is written: the peer has moved the balance and this node has not yet
        written the net, and a tick that read the row in between would charge it. The
        tick does not wait for the lock: it leaves the child to the next tick."""
        lock = delegation_lock(ALIAS)
        lock.acquire()
        self.peer.add(-100, "withdrawal", ts=self.now - 60)

        def answer(peer, client, token="", from_seq=0, rows=None):
            return self.peer.statement(
                from_seq=from_seq, to_seq=from_seq + rows - 1 if rows else 0, ts=self.now)

        with patch.object(maintain, "sc", self.sc), \
                patch.object(maintain, "_check_our_account"), \
                patch.object(statement_check, "read", MagicMock(side_effect=answer)), \
                patch.object(maintain, "_balance_held_on", return_value=10 ** 9), \
                patch.object(statement_check, "stored_rates", return_value={}), \
                patch.object(maintain, "get_client_id_on_other_peer", return_value="client"), \
                patch.object(maintain, "instance_balance_on_peer", return_value=self.peer.balance), \
                patch.object(maintain, "peer_mu_in_local", side_effect=lambda p, a, **k: a), \
                patch.object(maintain, "stop_instance"), \
                patch.object(delegation_commission, "commission_percent", return_value=0.0), \
                patch.object(self.sc, "child_statement_due", return_value=False):
            tick = threading.Thread(target=maintain.maintain_delegated_instances)
            tick.start()
            tick.join(10)
            self.assertFalse(tick.is_alive(), "the tick must not wait for modify_deposit")
            self.assertEqual(self.sc.get_delegated_instances()[0]["charged_peer_mu"], 0)
            self.sc.move_delegated_deposit(ALIAS, FATHER, -100, -100)
            lock.release()
            maintain.maintain_delegated_instances()

        row = self.sc.get_delegated_instances()[0]
        self.assertEqual(row["charged_peer_mu"], 0)
        self.assertEqual(self.deposit, 900 - 100)

    def test_the_first_reading_of_a_child_with_no_mark_does_not_charge_the_build_as_consumption(self):
        """The build that the peer took was more than the quote that the balance was
        measured from; what opened the account is not consumption."""
        SQLConnection._connection.execute(
            "UPDATE delegated_instances SET peer_statement_seq = NULL, "
            "peer_statement_hash = NULL, peer_statement_ts = NULL, peer_balance_mu = '950'")
        SQLConnection._connection.commit()

        charge = self.tick(statement=False)

        self.assertEqual(charge, 0)
        self.assertEqual(self.deposit, 900)
        row = self.sc.get_delegated_instances()[0]
        self.assertEqual((row["verified_consumption_peer_mu"], row["peer_statement_seq"]), (0, 2))

    def test_what_a_child_consumes_after_a_first_reading_with_no_mark_is_charged(self):
        SQLConnection._connection.execute(
            "UPDATE delegated_instances SET peer_statement_seq = NULL, "
            "peer_statement_hash = NULL, peer_statement_ts = NULL, peer_balance_mu = '950'")
        SQLConnection._connection.commit()
        self.consume(5)

        self.tick(statement=False)

        self.assertEqual(self.deposit, 900 - 5)

    def test_a_withdrawal_of_the_father_is_not_charged_as_consumption(self):
        self.consume(5)
        self.tick(statement=False)
        self.father_moves(-100)
        self.tick(statement=False)
        self.consume(5)
        self.tick(statement=True)

        # 900 less 100 that the father took out, less the 10 that the child spent.
        self.assertEqual(self.deposit, 900 - 100 - 10)
        self.assertEqual(self.sc.get_balance_by_client_id(FATHER), 5000 + 100)

    def test_a_top_up_of_the_father_hides_no_consumption(self):
        self.father_moves(200)
        self.consume(30)
        self.tick(statement=False)
        self.assertEqual(self.deposit, 900 + 200 - 30)

        self.tick(statement=True)

        self.assertEqual(self.deposit, 900 + 200 - 30)

    def test_a_statement_corrects_an_estimate_that_was_too_high(self):
        """The peer shows less than the balance said: the next charge gives MU back."""
        self.consume(10)
        self.tick(statement=False)
        self.assertEqual(self.deposit, 900 - 10)
        # A refund of 4 that the balance reading did not show at the time it was taken.
        self.peer.add(4, "refund", ts=self.now - 30)

        charge = self.tick(statement=True)

        self.assertEqual(charge, -4)
        self.assertEqual(self.deposit, 900 - 6)

    def test_an_estimate_that_is_too_low_is_corrected_upward(self):
        self.consume(10)
        self.tick(statement=False)
        # Consumption that the first balance reading did not show.
        self.consume(7)
        self.tick(statement=True)

        self.assertEqual(self.deposit, 900 - 17)

    def test_a_balance_that_rises_alone_never_gives_MU_back(self):
        """A peer that reports a higher balance than the truth credits nobody."""
        self.peer.add(500, "deposit", ts=self.now - 30)

        self.assertEqual(self.tick(statement=False), 0)
        self.assertEqual(self.deposit, 900)

    def test_a_top_up_that_lands_after_the_read_of_what_was_sent_moves_nothing_wrongly(self):
        """The father's net is read before the peer is asked: a top-up that the peer
        confirms in between is not yet in it, and the balance has risen by it."""
        row = self.sc.get_delegated_instances()[0]
        self.peer.add(200, "top_up", ts=self.now - 30)
        self.sc.move_delegated_deposit(ALIAS, FATHER, 200, 200)

        with patch.object(maintain, "sc", self.sc), \
                patch.object(maintain, "instance_balance_on_peer", return_value=self.peer.balance), \
                patch.object(self.sc, "child_statement_due", return_value=False):
            charge, _ = maintain._read_child(row)

        # The charge cannot be negative, and the next tick has the net: nothing is owed.
        self.assertEqual(charge, 0)
        self.assertEqual(self.tick(statement=False), 0)
        self.assertEqual(self.deposit, 900 + 200)

    def test_a_forced_read_closes_the_charge_before_a_stop(self):
        self.consume(9)

        row = self.sc.get_delegated_instances()[0]
        with patch.object(maintain, "sc", self.sc), \
                patch.object(statement_check, "read", MagicMock(
                    side_effect=lambda *a, from_seq=0, rows=None, **k: self.peer.statement(
                        from_seq=from_seq, to_seq=from_seq + rows - 1 if rows else 0,
                        ts=self.now))), \
                patch.object(maintain, "_balance_held_on", return_value=10 ** 9), \
                patch.object(statement_check, "stored_rates", return_value={}), \
                patch.object(maintain, "get_client_id_on_other_peer", return_value="client"), \
                patch.object(maintain, "peer_mu_in_local", side_effect=lambda p, a, **k: a), \
                patch.object(delegation_commission, "commission_percent", return_value=0.0), \
                patch.object(self.sc, "get_delegated_instances", return_value=[row]):
            maintain.close_delegated_charge(PEER_TOKEN)

        self.assertEqual(self.deposit, 900 - 9)

    def test_a_peer_that_rewrites_its_history_costs_it_the_chain_and_the_mark_goes_on(self):
        self.consume(5)
        self.tick(statement=True)
        # The peer shows another movement 3 than the one it showed.
        shown = self.peer.rows[2]["hash"]
        self.peer.rows[2] = dict(self.peer.rows[2], amount=-6, balance_after=self.peer.rows[1]["balance_after"] - 6,
                                 hash="rewritten")
        self.consume(5)

        with patch.object(statement_check, "penalise_once") as penalise:
            self.tick(statement=True)

        self.assertNotEqual(shown, "rewritten")
        penalise.assert_called()
        row = self.sc.get_delegated_instances()[0]
        self.assertEqual(row["peer_statement_seq"], len(self.peer.rows))


if __name__ == "__main__":
    unittest.main()
