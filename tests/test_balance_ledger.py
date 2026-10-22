"""The ledger of balance movements and GetBalanceStatement (#570).

Every write of a balance goes with its row, in one transaction, so the rows of an
account add up to its balance. A statement is charged to whoever asks, and is signed.
"""

import hashlib
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    from mnemonic import Mnemonic

    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.identity import node_identity
    from src.manager import balance_statement
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class _Database(unittest.TestCase):
    """An in-memory database with the real schema."""

    def setUp(self):
        self.sc = SQLConnection()
        self._original_connection = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original_connection

    def rows(self, account):
        return [dict(r) for r in SQLConnection._connection.execute(
            "SELECT * FROM balance_movements WHERE account_id = ? ORDER BY seq", (account,)
        ).fetchall()]

    def assert_adds_up(self, account, balance):
        rows = self.rows(account)
        self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows) + 1)))
        running = 0
        for row in rows:
            running += int(row["amount_mu"])
            self.assertEqual(int(row["balance_after_mu"]), running)
        self.assertEqual(running, balance)

    def assert_chained(self, account):
        """Each row hangs from the one before it, and its hash is the one of its fields."""
        previous = ledger.genesis_hash(account)
        for row in self.rows(account):
            self.assertEqual(row["prev_hash"], previous)
            self.assertEqual(row["hash"], ledger.movement_hash(
                row["prev_hash"], row["seq"], row["ts"], row["amount_mu"],
                row["balance_after_mu"], row["kind"], row["instance_ref"], row["detail"],
            ))
            previous = row["hash"]

    def add_instance(self, token, father, balance, movements=None):
        self.sc.add_local_instance(
            father_id=father, container_ip="10.0.0.2", container_id=token, name=token,
            balance_mu=balance, serialized_instance="", service_id="s", virtualizer="ch",
            disk_space=0, envs="", movements=movements,
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerWriteTests(_Database):

    def test_a_new_client_opens_with_its_credit(self):
        self.sc.add_client("c1", 500, None)

        self.assertEqual(self.rows("c1")[0]["kind"], ledger.CREDIT)
        self.assert_adds_up("c1", 500)

    def test_every_move_writes_a_row_that_adds_up(self):
        self.sc.add_client("c1", 500, None)
        self.sc.move_balance("c1", 200, ledger.DEPOSIT)
        self.sc.move_balance("c1", -300, ledger.CHILD_START, instance_token="child")

        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 400)
        self.assert_adds_up("c1", 400)
        self.assertEqual(self.rows("c1")[-1]["instance_ref"], ledger.instance_ref("child"))
        self.assert_chained("c1")

    def test_a_charge_beyond_the_balance_moves_nothing_without_debt(self):
        self.sc.add_client("c1", 100, None)

        with self.assertRaises(ledger.InsufficientBalance):
            self.sc.move_balance("c1", -101, ledger.TOP_UP, allow_debt=False)

        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 100)
        self.assertEqual(len(self.rows("c1")), 1)

    def test_an_unknown_account_raises(self):
        with self.assertRaises(ledger.NoSuchAccount):
            self.sc.move_balance("nobody", 1, ledger.DEPOSIT)

    def test_an_amount_of_zero_writes_nothing(self):
        self.sc.add_client("c1", 100, None)

        self.assertIsNone(self.sc.move_balance("c1", 0, ledger.STATEMENT_QUERY))
        self.assertEqual(len(self.rows("c1")), 1)

    def test_a_new_instance_opens_with_its_start_and_build(self):
        self.add_instance("i1", "c1", 700, movements=(
            ledger.Movement(ledger.START, 1000), ledger.Movement(ledger.BUILD, -300),
        ))

        self.assertEqual([r["kind"] for r in self.rows("i1")], [ledger.START, ledger.BUILD])
        self.assert_adds_up("i1", 700)

    def test_opening_movements_must_add_up_to_the_balance(self):
        with self.assertRaises(ValueError):
            self.add_instance("i1", "c1", 700, movements=(ledger.Movement(ledger.START, 1000),))
        self.assertFalse(self.sc.internal_instance_exists("i1"))

    def test_a_transfer_moves_both_sides_or_neither(self):
        self.sc.add_client("c1", 100, None)
        self.add_instance("i1", "c1", 50)

        self.sc.transfer("c1", "i1", 60, ledger.TOP_UP, child="i1")
        with self.assertRaises(ledger.InsufficientBalance):
            self.sc.transfer("c1", "i1", 60, ledger.TOP_UP, child="i1")

        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 40)
        self.assertEqual(self.sc.get_instance_balance("i1"), 110)
        self.assertEqual(self.rows("c1")[-1]["instance_ref"], ledger.instance_ref("i1"))
        self.assertIsNone(self.rows("i1")[-1]["instance_ref"])
        self.assert_adds_up("c1", 40)
        self.assert_adds_up("i1", 110)

    def test_the_child_of_a_start_is_bound_by_a_row_of_its_own(self):
        self.sc.add_client("c1", 100, None)
        seq = self.sc.move_balance("c1", -10, ledger.CHILD_START)
        start = self.rows("c1")[-1]

        bound_seq = self.sc.bind_child("c1", seq, "child-1")

        self.assertEqual(self.rows("c1")[seq - 1], start, "the start row is never changed")
        bound = self.rows("c1")[-1]
        self.assertEqual(bound["seq"], bound_seq)
        self.assertEqual(bound["kind"], ledger.CHILD_BOUND)
        self.assertEqual(int(bound["amount_mu"]), 0)
        self.assertEqual(int(bound["balance_after_mu"]), 90)
        self.assertEqual(bound["instance_ref"], ledger.instance_ref("child-1"))
        self.assertEqual(bound["detail"], '{"starts_seq":"%d"}' % seq)
        self.assert_adds_up("c1", 90)
        self.assert_chained("c1")

    def test_a_delegated_instance_is_an_account_under_the_alias_of_its_father(self):
        self.sc.add_client("c1", 100, None)
        self.sc.add_delegated_instance(
            father_id="c1", encrypted_external_token="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=60,
            peer_balance_mu=6, movements=(
                ledger.Movement(ledger.START, 100), ledger.Movement(ledger.COMMISSION, -10),
                ledger.Movement(ledger.BUILD, -30),
            ), sent_peer_mu=9, mark={"seq": 2, "ts": 5, "read_at": 7.0},
        )

        self.sc.settle_delegated_tick("peer-token", 15, {"peer_balance_mu": 4, "seq": 3})

        self.assertEqual(self.sc.get_delegated_balance("peer-token"), 45)
        self.assert_adds_up("alias", 45)
        row = self.sc.get_delegated_instances()[0]
        self.assertEqual((row["peer_balance_mu"], row["peer_statement_seq"]), (4, 3))
        self.assertEqual(
            self.sc.delegated_starts_on_peer("p"), {ledger.instance_ref("peer-token"): 9}
        )

    def test_the_sweep_keeps_the_last_row_of_a_live_account(self):
        self.sc.add_client("c1", 100, None)
        self.sc.move_balance("c1", 5, ledger.DEPOSIT)
        SQLConnection._connection.execute("UPDATE balance_movements SET ts = 0")
        SQLConnection._connection.commit()

        self.assertEqual(self.sc.prune_balance_movements(max_age_days=30), 1)
        self.sc.move_balance("c1", 5, ledger.DEPOSIT)

        self.assertEqual([r["seq"] for r in self.rows("c1")], [2, 3])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerChainTests(_Database):

    def test_the_first_row_hangs_from_the_genesis_of_its_account(self):
        self.sc.add_client("c1", 500, None)
        self.sc.add_client("c2", 500, None)

        self.assertEqual(self.rows("c1")[0]["prev_hash"], ledger.genesis_hash("c1"))
        self.assertNotEqual(ledger.genesis_hash("c1"), ledger.genesis_hash("c2"))
        self.assert_chained("c1")

    def test_every_row_hangs_from_the_one_before_it(self):
        self.sc.add_client("c1", 500, None)
        for amount in (-10, 25, -3):
            self.sc.move_balance("c1", amount, ledger.TOP_UP if amount < 0 else ledger.DEPOSIT)

        rows = self.rows("c1")
        self.assertEqual(len(rows), 4)
        for before, row in zip(rows, rows[1:]):
            self.assertEqual(row["prev_hash"], before["hash"])
        self.assert_chained("c1")

    def test_the_chain_survives_the_sweep(self):
        self.sc.add_client("c1", 100, None)
        self.sc.move_balance("c1", 5, ledger.DEPOSIT)
        SQLConnection._connection.execute("UPDATE balance_movements SET ts = 0")
        SQLConnection._connection.commit()
        kept = self.rows("c1")[1]["hash"]

        self.sc.prune_balance_movements(max_age_days=30)
        self.sc.move_balance("c1", 5, ledger.DEPOSIT)

        rows = self.rows("c1")
        self.assertEqual([r["seq"] for r in rows], [2, 3])
        self.assertEqual(rows[1]["prev_hash"], kept)

    def test_a_row_is_the_same_hash_for_any_implementation(self):
        """A fixed vector: another implementation of the chain can check itself with it."""
        genesis = ledger.genesis_hash("acct")
        self.assertEqual(
            genesis, "4d754ff1bc79afaa5f564f5f8afc57addbf565e6ea85946592a622db18df05c2")
        self.assertEqual(
            ledger.movement_hash(genesis, 1, 1700000000, 500, 500, "credit", None, None),
            "38683a3a15dc9ae892fc745acd9a3592d7bb55ea2ed48c7519e25fc8fcc07430")
        self.assertEqual(
            ledger.movement_hash("ab" * 32, 2, 1700000001, -10, 490, "child_start",
                                 ledger.instance_ref("tok"), '{"rows":"3"}'),
            "fd0410b88cd86147c5837919fc46247fd5fcfca4a43bf2d4322e7b7c253fc34c")

    def test_a_field_cannot_pass_for_a_separator(self):
        """Two rows that differ in where a text ends never give the same hash."""
        a = ledger.movement_hash("p", 1, 5, 1, 1, "x~y", None, None)
        b = ledger.movement_hash("p", 1, 5, 1, 1, "x", "y", None)
        self.assertNotEqual(a, b)

    def test_a_reference_is_not_the_alias_of_a_delegated_instance(self):
        self.assertNotEqual(ledger.instance_ref("tok"), hashlib.sha256(b"tok").hexdigest())
        self.assertNotEqual(ledger.instance_ref("x"), ledger.account_ref("x"))

    def test_no_row_holds_a_credential(self):
        self.sc.add_client("secret-client", 500, None)
        self.sc.move_balance("secret-client", -10, ledger.CHILD_START, instance_token="secret-token")
        self.sc.bind_child("secret-client", 2, "secret-token")

        for row in self.rows("secret-client"):
            for value in row.values():
                self.assertNotIn("secret-token", str(value))
        self.assertEqual(self.rows("secret-client")[1]["instance_ref"],
                         ledger.instance_ref("secret-token"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SettleStopTests(_Database):

    def setUp(self):
        super().setUp()
        self.sc.add_client("c1", 1000, None)
        self.add_instance("i1", "c1", 400, movements=(ledger.Movement(ledger.START, 400),))

    def test_the_leftover_goes_to_the_father_in_one_step_and_the_child_dies_empty(self):
        self.assertEqual(self.sc.settle_stop("i1", "manual_kill", father_id="c1"), 400)

        child, father = self.rows("i1"), self.rows("c1")
        self.assertEqual((child[-1]["kind"], int(child[-1]["amount_mu"])), (ledger.REFUND, -400))
        self.assertEqual(int(child[-1]["balance_after_mu"]), 0)
        self.assertEqual((father[-1]["kind"], int(father[-1]["amount_mu"])), (ledger.REFUND, 400))
        self.assertEqual(father[-1]["instance_ref"], ledger.instance_ref("i1"))
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1400)
        self.assertFalse(self.sc.internal_instance_exists("i1"))
        self.assert_adds_up("i1", 0)
        self.assert_chained("i1")
        self.assert_chained("c1")

    def test_a_stop_that_repeats_does_nothing(self):
        self.sc.settle_stop("i1", "manual_kill", father_id="c1")

        self.assertIsNone(self.sc.settle_stop("i1", "manual_kill", father_id="c1"))
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1400)

    def test_a_dead_instance_cannot_be_charged(self):
        self.sc.settle_stop("i1", "manual_kill", father_id="c1")

        with self.assertRaises(ledger.NoSuchAccount):
            self.sc.move_balance("i1", -1, ledger.MAINTENANCE)

    def test_an_unknown_instance_is_not_settled(self):
        self.assertIsNone(self.sc.settle_stop("nobody", "manual_kill", father_id="c1"))

    def test_a_father_that_is_gone_loses_nothing_that_the_books_do_not_say(self):
        self.assertEqual(self.sc.settle_stop("i1", "manual_kill", father_id="gone"), 400)

        refund = self.rows("i1")[-1]
        self.assertEqual(refund["detail"], '{"lost":"father_gone"}')
        self.assertFalse(self.sc.internal_instance_exists("i1"))
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1000)

    def test_a_caller_that_takes_the_leftover_gets_the_figure_and_the_father_nothing(self):
        self.assertEqual(
            self.sc.settle_stop("i1", "burnall", father_id="c1", credit=False), 400)

        self.assertEqual(self.rows("i1")[-1]["detail"], '{"to":"caller"}')
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1000)
        self.assertFalse(self.sc.internal_instance_exists("i1"))

    def test_an_instance_in_debt_dies_with_its_debt_and_no_refund(self):
        self.sc.move_balance("i1", -450, ledger.MAINTENANCE)

        self.assertEqual(self.sc.settle_stop("i1", "out_of_balance", father_id="c1"), 0)

        self.assertEqual([r["kind"] for r in self.rows("i1")][-1], ledger.MAINTENANCE)
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1000)
        self.assertFalse(self.sc.internal_instance_exists("i1"))

    def test_an_instance_father_is_credited_too(self):
        self.add_instance("i2", "i1", 100, movements=(ledger.Movement(ledger.START, 100),))

        self.sc.settle_stop("i2", "manual_kill", father_id="i1")

        self.assertEqual(self.sc.get_instance_balance("i1"), 500)
        self.assert_adds_up("i1", 500)

    def test_a_delegated_instance_settles_under_its_alias(self):
        self.sc.add_delegated_instance(
            father_id="c1", encrypted_external_token="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=60,
            peer_balance_mu=6, movements=(
                ledger.Movement(ledger.START, 100), ledger.Movement(ledger.COMMISSION, -10),
                ledger.Movement(ledger.BUILD, -30),
            ), sent_peer_mu=9, mark={},
        )

        self.assertEqual(
            self.sc.settle_stop("alias", "manual_kill", delegated=True, father_id="c1"), 60)

        self.assertEqual(self.rows("alias")[-1]["kind"], ledger.REFUND)
        self.assert_adds_up("alias", 0)
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1060)

    def test_a_charge_that_lands_during_the_stop_is_never_refunded_too(self):
        """Whatever the interleaving, what the child had splits into what it was charged
        and what its father got back: no MU appear."""
        charged = []
        lock = threading.Lock()

        def charge():
            for _ in range(40):
                try:
                    self.sc.move_balance("i1", -5, ledger.TUNNEL, allow_debt=False)
                except ledger.NoSuchAccount:
                    return
                except ledger.InsufficientBalance:
                    return
                with lock:
                    charged.append(5)

        threads = [threading.Thread(target=charge) for _ in range(4)]
        for thread in threads:
            thread.start()
        refund = self.sc.settle_stop("i1", "manual_kill", father_id="c1")
        for thread in threads:
            thread.join()

        self.assertEqual(sum(charged) + refund, 400)
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1000 + refund)
        self.assert_adds_up("i1", 0)
        self.assert_chained("i1")


def _write_from_another_process(path, account, count):
    """What the CLI does: write to the same file, from a process of its own."""
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    SQLConnection._connection = connection
    sc = SQLConnection()
    for _ in range(count):
        sc.move_balance(account, 1, ledger.DEPOSIT)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WritersInOtherProcessesTests(unittest.TestCase):

    def test_two_processes_and_this_one_keep_the_chain_and_the_sum(self):
        original = SQLConnection._connection
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nodo.db")
            connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            create_tables(connection.cursor())
            connection.commit()
            SQLConnection._connection = connection
            try:
                sc = SQLConnection()
                sc.add_client("c1", 0, None)
                context = multiprocessing.get_context("spawn")
                workers = [
                    context.Process(target=_write_from_another_process, args=(path, "c1", 60))
                    for _ in range(2)
                ]
                for worker in workers:
                    worker.start()
                for _ in range(60):
                    sc.move_balance("c1", 1, ledger.DEPOSIT)
                for worker in workers:
                    worker.join(timeout=120)
                    self.assertEqual(worker.exitcode, 0)

                rows = [dict(r) for r in connection.execute(
                    "SELECT * FROM balance_movements WHERE account_id = 'c1' ORDER BY seq")]
                self.assertEqual([r["seq"] for r in rows], list(range(1, 181)))
                previous = ledger.genesis_hash("c1")
                for row in rows:
                    self.assertEqual(row["prev_hash"], previous)
                    previous = row["hash"]
                self.assertEqual(sc.get_balance_by_client_id("c1"), 180)
                self.assertEqual(int(rows[-1]["balance_after_mu"]), 180)
            finally:
                connection.close()
                SQLConnection._connection = original


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedDepositRowsTests(_Database):
    """``ModifyDeposit`` on a delegated instance: its rows and the net sent to the peer."""

    def setUp(self):
        super().setUp()
        self.sc.add_client("c1", 1000, None)
        self.sc.add_delegated_instance(
            father_id="c1", encrypted_external_token="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=500,
            peer_balance_mu=1000, movements=(ledger.Movement(ledger.START, 500),),
            sent_peer_mu=1000, mark={},
        )

    def net(self):
        return self.sc.get_delegated_instances()[0]["father_net_peer_mu"]

    def test_a_top_up_is_a_row_on_the_instance_and_a_positive_net(self):
        self.sc.move_delegated_deposit("alias", "c1", 100, 200)

        self.assertEqual(self.sc.get_delegated_balance("peer-token"), 600)
        self.assertEqual(self.rows("alias")[-1]["kind"], ledger.TOP_UP)
        self.assertEqual(self.net(), 200)
        self.assert_adds_up("alias", 600)

    def test_a_withdrawal_is_a_row_on_each_side_and_a_negative_net(self):
        self.sc.move_delegated_deposit("alias", "c1", -100, -200)

        self.assertEqual(self.sc.get_delegated_balance("peer-token"), 400)
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1100)
        self.assertEqual(self.rows("c1")[-1]["instance_ref"], ledger.instance_ref("alias"))
        self.assertEqual(self.net(), -200)
        self.assert_chained("alias")
        self.assert_chained("c1")

    def test_the_net_is_the_sum_of_what_was_sent(self):
        self.sc.move_delegated_deposit("alias", "c1", 100, 200)
        self.sc.move_delegated_deposit("alias", "c1", -50, -100)
        self.sc.move_delegated_deposit("alias", "c1", 10, 20)

        self.assertEqual(self.net(), 120)

    def test_a_withdrawal_to_a_father_that_is_gone_writes_nothing_and_leaves_the_net(self):
        with self.assertRaises(ledger.NoSuchAccount):
            self.sc.move_delegated_deposit("alias", "gone", -100, -200)

        self.assertEqual(self.sc.get_delegated_balance("peer-token"), 500)
        self.assertEqual(self.net(), 0)

    def test_a_correction_is_a_row_with_its_detail_and_updates_the_totals(self):
        self.sc.settle_delegated_tick(
            "peer-token", 30, {"peer_balance_mu": 970, "verified_consumption": 30,
                               "verified_father_net": 0, "charged_delta": 30})
        self.sc.settle_delegated_tick(
            "peer-token", 0, {"peer_balance_mu": 975, "verified_consumption": 25,
                              "charged_delta": -5}, refund_mu=5, detail={"peer_id": "p"})

        row = self.sc.get_delegated_instances()[0]
        self.assertEqual((row["verified_consumption_peer_mu"], row["charged_peer_mu"]), (25, 25))
        self.assertEqual(self.sc.get_delegated_balance("peer-token"), 475)
        last = self.rows("alias")[-1]
        self.assertEqual((last["kind"], int(last["amount_mu"])), (ledger.MAINTENANCE, 5))
        self.assertIn('"adjustment":"1"', last["detail"])
        self.assert_adds_up("alias", 475)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StatementBooksTests(_Database):
    """What a node keeps about the statements of a peer: ticks, counters, mark."""

    def setUp(self):
        super().setUp()
        SQLConnection._connection.execute("INSERT INTO peer (id) VALUES ('p')")
        SQLConnection._connection.commit()

    def test_a_read_is_due_every_so_many_ticks(self):
        due = [self.sc.statement_tick_due("p", 3) for _ in range(7)]

        self.assertEqual(due, [False, False, True, False, False, True, False])

    def test_a_peer_that_is_not_known_is_never_due(self):
        self.assertFalse(self.sc.statement_tick_due("nobody", 1))

    def test_the_counters_only_go_up(self):
        self.sc.add_statement_read("p", rows_priced=1)
        self.sc.add_statement_read("p", rows_priced=4, count=0)
        self.sc.add_statement_queries_verified("p", 1, 14)
        self.sc.add_statement_queries_verified("p", 2, 30)

        self.assertEqual(self.sc.get_statement_counters("p"),
                         {"reads": 1, "rows_priced": 5, "verified": 3, "verified_mu": 44})

    def test_the_mark_of_a_peer_is_kept_whole(self):
        self.assertEqual(self.sc.get_peer_statement_mark("p"), {})

        self.sc.set_peer_statement_mark("p", 7, "abc", 1234, 99)

        self.assertEqual(self.sc.get_peer_statement_mark("p"),
                         {"seq": 7, "hash": "abc", "balance_mu": 1234, "ts": 99})

    def test_the_interval_of_reads_is_held_to_a_tenth_of_the_shortest_retention(self):
        with patch.object(ledger, "min_peer_retention_days", return_value=1):
            # A tenth of a day is 8640 s: 864 ticks of 10 s.
            with patch.object(ledger, "_positive_int", return_value=10 ** 9):
                self.assertEqual(ledger.statement_read_every_ticks(10), 864)
            with patch.object(ledger, "_positive_int", return_value=30):
                self.assertEqual(ledger.statement_read_every_ticks(10), 30)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StatementReadTests(_Database):

    def setUp(self):
        super().setUp()
        self.sc.add_client("c1", 1000, None)
        for _ in range(5):
            self.sc.move_balance("c1", -10, ledger.TOP_UP)

    def read(self, account="c1", payer="c1", **kwargs):
        kwargs.setdefault("price", lambda rows: 1 + rows)
        return self.sc.balance_statement(account, payer, **kwargs)

    def test_the_charge_is_the_last_movement_when_the_answer_reaches_the_head(self):
        read = self.read(from_seq=1)

        self.assertEqual([m["seq"] for m in read["movements"]], list(range(1, 8)))
        last = read["movements"][-1]
        self.assertEqual(last["kind"], ledger.STATEMENT_QUERY)
        # Six rows before it and itself: 1 + 7.
        self.assertEqual(int(last["amount_mu"]), -8)
        self.assertEqual(int(last["balance_after_mu"]), read["balance_mu"])
        self.assertEqual((read["head_seq"], read["head_hash"]), (7, last["hash"]))
        self.assert_adds_up("c1", read["balance_mu"])
        self.assert_chained("c1")

    def test_a_range_gives_those_movements_and_the_charge_is_in_the_head(self):
        read = self.read(from_seq=2, to_seq=4)

        self.assertEqual([m["seq"] for m in read["movements"]], [2, 3, 4])
        self.assertEqual(read["head_seq"], 7)
        self.assertEqual(self.rows("c1")[-1]["kind"], ledger.STATEMENT_QUERY)

    def test_from_zero_reads_only_the_head_and_pays_only_the_fixed_part(self):
        read = self.read(from_seq=0)

        self.assertEqual(read["movements"], [])
        self.assertEqual(int(self.rows("c1")[-1]["amount_mu"]), -2)
        self.assertEqual((read["head_seq"], read["head_hash"]),
                         (7, self.rows("c1")[-1]["hash"]))
        self.assertEqual(read["oldest_seq"], 1)

    def test_the_node_cap_gives_the_first_rows_and_the_next_page_goes_on(self):
        first = self.read(from_seq=1, max_rows=3)
        self.assertEqual([m["seq"] for m in first["movements"]], [1, 2, 3])

        second = self.read(from_seq=4, max_rows=3)
        self.assertEqual([m["seq"] for m in second["movements"]], [4, 5, 6])

        third = self.read(from_seq=7, max_rows=3)
        # The charge of the first read, of the second, and of this one.
        self.assertEqual([m["seq"] for m in third["movements"]], [7, 8, 9])

    def test_a_read_past_the_head_gets_only_the_charge(self):
        read = self.read(from_seq=100)

        self.assertEqual(read["movements"], [])

    def test_a_read_that_starts_at_the_next_movement_gets_the_charge(self):
        read = self.read(from_seq=7)

        self.assertEqual([m["seq"] for m in read["movements"]], [7])
        self.assertEqual(read["movements"][0]["kind"], ledger.STATEMENT_QUERY)

    def test_what_the_sweep_deleted_is_where_the_answer_starts(self):
        SQLConnection._connection.execute("UPDATE balance_movements SET ts = 0")
        SQLConnection._connection.commit()
        self.sc.prune_balance_movements(max_age_days=30)

        read = self.read(from_seq=1)

        self.assertEqual(read["oldest_seq"], 6)
        self.assertEqual([m["seq"] for m in read["movements"]], [6, 7])
        self.assertEqual(read["movements"][0]["prev_hash"], self.rows("c1")[0]["prev_hash"])

    def test_a_father_reads_its_child_and_pays_for_it(self):
        self.add_instance("i1", "c1", 70, movements=(ledger.Movement(ledger.START, 70),))

        read = self.read(account="i1", payer="c1", from_seq=1)

        self.assertEqual([m["kind"] for m in read["movements"]], [ledger.START])
        self.assertEqual(read["balance_mu"], 70)
        charge = self.rows("c1")[-1]
        self.assertEqual((charge["kind"], charge["instance_ref"]),
                         (ledger.STATEMENT_QUERY, ledger.instance_ref("i1")))
        self.assertEqual(int(charge["amount_mu"]), -2)

    def test_an_account_that_does_not_exist_is_not_charged(self):
        with self.assertRaises(ledger.NoSuchAccount):
            self.read(account="nobody", payer="c1", from_seq=1)
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 950)

    def test_a_payer_that_cannot_pay_gets_nothing(self):
        with self.assertRaises(ledger.InsufficientBalance):
            self.read(price=lambda rows: 10 ** 9, allow_debt=False, from_seq=1)
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 950)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SignedStatementTests(_Database):

    def setUp(self):
        super().setUp()
        self.mnemonic = Mnemonic("english").generate()
        patcher = patch.object(node_identity, "get_identity_mnemonic", return_value=self.mnemonic)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.peer_id = node_identity.get_node_public_key_hex()
        self.sc.add_client("c1", 1000, None)
        self.add_instance("i1", "c1", 1000, movements=(ledger.Movement(ledger.START, 1000),))
        self.add_instance("i2", "someone-else", 1000,
                          movements=(ledger.Movement(ledger.START, 1000),))

    def test_the_answer_verifies_against_the_key_of_the_node(self):
        statement = balance_statement.answer(caller="c1", from_seq=1)

        self.assertEqual(statement.account_ref, ledger.account_ref("c1"))
        self.assertTrue(balance_statement.verify(statement, self.peer_id))

    def test_a_changed_movement_breaks_the_signature(self):
        statement = balance_statement.answer(caller="c1", from_seq=1)
        statement.movements[0].amount.n = "999999"

        self.assertFalse(balance_statement.verify(statement, self.peer_id))

    def test_a_movement_that_is_dropped_breaks_the_signature(self):
        self.sc.move_balance("c1", -1, ledger.TOP_UP)
        statement = balance_statement.answer(caller="c1", from_seq=1)
        statement.movements.pop(0)

        self.assertFalse(balance_statement.verify(statement, self.peer_id))

    def test_the_head_is_signed(self):
        statement = balance_statement.answer(caller="c1", from_seq=0)
        statement.head_hash = "0" * 64

        self.assertFalse(balance_statement.verify(statement, self.peer_id))

    def test_the_hash_of_a_statement_movement_is_the_one_of_its_row(self):
        statement = balance_statement.answer(caller="c1", from_seq=1)

        rows = self.rows("c1")
        self.assertEqual([balance_statement.movement_hash(m) for m in statement.movements],
                         [row["hash"] for row in rows])
        self.assertEqual(statement.head_hash, rows[-1]["hash"])

    def test_a_statement_holds_no_credential(self):
        self.sc.add_client("a-client-id-that-is-a-secret", 500, None)
        self.sc.move_balance("a-client-id-that-is-a-secret", -10, ledger.CHILD_START,
                             instance_token="a-token-that-is-a-secret")
        self.sc.bind_child("a-client-id-that-is-a-secret", 2, "a-token-that-is-a-secret")

        statement = balance_statement.answer(caller="a-client-id-that-is-a-secret", from_seq=1)

        wire = statement.SerializeToString()
        self.assertNotIn(b"secret", wire)
        self.assertEqual(statement.account_ref, ledger.account_ref("a-client-id-that-is-a-secret"))

    def test_a_caller_reads_its_child_by_token(self):
        statement = balance_statement.answer(caller="c1", token="i1", from_seq=1)

        self.assertEqual(statement.account_ref, ledger.account_ref("i1"))
        self.assertTrue(balance_statement.verify(statement, self.peer_id))

    def test_a_caller_cannot_read_an_account_that_is_not_its_own_or_a_child(self):
        with self.assertRaises(balance_statement.StatementRefused):
            balance_statement.answer(caller="c1", token="i2", from_seq=1)

    def test_a_child_reads_its_own_account_and_not_its_father(self):
        self.assertEqual(balance_statement.answer(caller="i1", from_seq=1).account_ref,
                         ledger.account_ref("i1"))
        with self.assertRaises(balance_statement.StatementRefused):
            balance_statement.answer(caller="i1", token="c1", from_seq=1)

    def test_an_instance_delegated_to_a_peer_has_no_statement(self):
        self.sc.add_delegated_instance(
            father_id="c1", encrypted_external_token="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=60,
            peer_balance_mu=6, movements=(ledger.Movement(ledger.START, 60),),
            sent_peer_mu=9, mark={},
        )
        before = len(self.rows("c1"))

        with self.assertRaises(balance_statement.StatementUnavailable):
            balance_statement.answer(caller="c1", token="alias", from_seq=1)
        self.assertEqual(len(self.rows("c1")), before, "nothing is charged for it")


if __name__ == "__main__":
    unittest.main()
