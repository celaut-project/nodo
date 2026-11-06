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
import json
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

    def test_a_local_child_is_linked_from_the_statement_of_its_father(self):
        self.sc.add_client("c1", 100, None)
        seq = self.sc.move_balance("c1", -10, ledger.CHILD_START)
        self.add_instance("child-1", "c1", 10, movements=(ledger.Movement(ledger.START, 10),))
        self.sc.bind_child("c1", seq, "child-1")

        link = [r for r in self.rows("c1") if r["kind"] == ledger.CHILD_BOUND][0]

        # The link is the name of the account of the child, as the ledger computes it.
        self.assertEqual(link["instance_ref"], ledger.instance_ref("child-1"))
        self.assertEqual(self.rows("child-1")[0]["account_id"], "child-1")
        self.assertEqual(link["detail"], '{"starts_seq":"%d"}' % seq)
        for row in self.rows("c1"):
            self.assertNotIn("child-1", " ".join(str(v) for v in row.values()))

    def test_a_delegated_child_is_linked_by_its_alias_and_never_by_the_peer_token(self):
        self.sc.add_client("c1", 100, None)
        seq = self.sc.move_balance("c1", -10, ledger.CHILD_START)
        self.sc.add_delegated_instance(
            father_id="c1", alias="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=10,
            peer_balance_mu=1, movements=(ledger.Movement(ledger.START, 10),),
            sent_peer_mu=0, mark=None,
        )
        self.sc.bind_child("c1", seq, "alias")

        link = [r for r in self.rows("c1") if r["kind"] == ledger.CHILD_BOUND][0]

        self.assertEqual(link["instance_ref"], ledger.instance_ref("alias"))
        self.assertNotEqual(link["instance_ref"], ledger.instance_ref("peer-token"))
        self.assertEqual(self.rows("alias")[0]["account_id"], "alias")

    def test_a_delegated_instance_is_an_account_under_the_alias_of_its_father(self):
        self.sc.add_client("c1", 100, None)
        self.sc.add_delegated_instance(
            father_id="c1", alias="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=60,
            peer_balance_mu=6, movements=(
                ledger.Movement(ledger.START, 100), ledger.Movement(ledger.BUILD, -40),
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
class ClientExpiryTests(_Database):
    """A client that fathers live instances is not deleted when it expires (#604)."""

    def setUp(self):
        super().setUp()
        self.sc.add_client("c1", 0, time.time() - 10 * 3600)

    def maintain(self):
        from src.manager.maintain import maintain_clients
        maintain_clients()

    def test_a_client_without_children_is_deleted(self):
        self.maintain()

        self.assertFalse(self.sc.client_exists("c1"))

    def test_a_client_with_a_live_local_child_is_kept(self):
        self.add_instance("i1", "c1", 400)

        self.maintain()

        self.assertTrue(self.sc.client_exists("c1"))

    def test_a_client_with_only_killed_children_is_deleted(self):
        self.add_instance("i1", "c1", 0)
        self.sc.settle_stop("i1", "manual_kill", father_id="c1")

        self.maintain()

        self.assertFalse(self.sc.client_exists("c1"))

    def test_a_client_with_a_live_delegated_child_is_kept(self):
        self.sc.add_delegated_instance(
            father_id="c1", alias="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=0,
        )

        self.maintain()

        self.assertTrue(self.sc.client_exists("c1"))

    def test_a_client_with_a_running_start_is_kept(self):
        self.sc._execute(
            "INSERT INTO start_refs (caller_id, start_ref, ts, state)"
            " VALUES ('c1', 'r', 1, 'running')")

        self.maintain()

        self.assertTrue(self.sc.client_exists("c1"))

    def test_the_refund_of_a_child_that_stops_later_goes_to_its_father(self):
        self.add_instance("i1", "c1", 400, movements=(ledger.Movement(ledger.START, 400),))

        self.maintain()
        self.assertEqual(self.sc.settle_stop("i1", "manual_kill", father_id="c1"), 400)

        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 400)
        self.assertNotEqual(self.rows("i1")[-2]["detail"], '{"lost":"father_gone"}')


class SettleStopTests(_Database):

    def setUp(self):
        super().setUp()
        self.sc.add_client("c1", 1000, None)
        self.add_instance("i1", "c1", 400, movements=(ledger.Movement(ledger.START, 400),))

    def test_the_chain_of_a_stopped_instance_ends_with_a_stop_row(self):
        self.sc.settle_stop("i1", "out_of_balance", father_id="c1")

        last = self.rows("i1")[-1]
        self.assertEqual((last["kind"], int(last["amount_mu"]), int(last["balance_after_mu"])),
                         (ledger.STOP, 0, 0))
        self.assertEqual(json.loads(last["detail"]),
                         {"reason": "out_of_balance", "leftover": "400"})
        self.assertIsNone(last["instance_ref"])
        self.assert_chained("i1")
        self.assert_adds_up("i1", 0)

    def test_an_instance_with_no_leftover_still_gets_a_stop_row(self):
        self.sc.settle_stop("i1", "manual_kill", father_id="c1")
        self.add_instance("i2", "c1", 0)

        self.sc.settle_stop("i2", "manual_kill", father_id="c1")

        self.assertEqual([r["kind"] for r in self.rows("i2")], [ledger.STOP])
        self.assertEqual(json.loads(self.rows("i2")[0]["detail"])["leftover"], "0")

    def test_a_debt_stays_on_the_stop_row(self):
        self.add_instance("i3", "c1", -30)

        self.sc.settle_stop("i3", "manual_kill", father_id="c1")

        last = self.rows("i3")[-1]
        self.assertEqual((last["kind"], int(last["balance_after_mu"])), (ledger.STOP, -30))

    def test_a_second_stop_writes_no_second_stop_row(self):
        self.sc.settle_stop("i1", "manual_kill", father_id="c1")
        self.assertIsNone(self.sc.settle_stop("i1", "manual_kill", father_id="c1"))

        self.assertEqual([r["kind"] for r in self.rows("i1")].count(ledger.STOP), 1)

    def test_the_leftover_goes_to_the_father_in_one_step_and_the_child_dies_empty(self):
        self.assertEqual(self.sc.settle_stop("i1", "manual_kill", father_id="c1"), 400)

        child, father = self.rows("i1"), self.rows("c1")
        self.assertEqual((child[-2]["kind"], int(child[-2]["amount_mu"])), (ledger.REFUND, -400))
        self.assertEqual(int(child[-2]["balance_after_mu"]), 0)
        self.assertEqual(child[-1]["kind"], ledger.STOP)
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

        refund = self.rows("i1")[-2]
        self.assertEqual(refund["detail"], '{"lost":"father_gone"}')
        self.assertFalse(self.sc.internal_instance_exists("i1"))
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1000)

    def test_a_caller_that_takes_the_leftover_gets_the_figure_and_the_father_nothing(self):
        self.assertEqual(
            self.sc.settle_stop("i1", "burnall", father_id="c1", credit=False), 400)

        self.assertEqual(self.rows("i1")[-2]["detail"], '{"to":"caller"}')
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1000)
        self.assertFalse(self.sc.internal_instance_exists("i1"))

    def test_an_instance_in_debt_dies_with_its_debt_and_no_refund(self):
        self.sc.move_balance("i1", -450, ledger.MAINTENANCE)

        self.assertEqual(self.sc.settle_stop("i1", "out_of_balance", father_id="c1"), 0)

        self.assertEqual([r["kind"] for r in self.rows("i1")][-2], ledger.MAINTENANCE)
        self.assertEqual(self.sc.get_balance_by_client_id("c1"), 1000)
        self.assertFalse(self.sc.internal_instance_exists("i1"))

    def test_an_instance_father_is_credited_too(self):
        self.add_instance("i2", "i1", 100, movements=(ledger.Movement(ledger.START, 100),))

        self.sc.settle_stop("i2", "manual_kill", father_id="i1")

        self.assertEqual(self.sc.get_instance_balance("i1"), 500)
        self.assert_adds_up("i1", 500)

    def test_a_delegated_instance_settles_under_its_alias(self):
        self.sc.add_delegated_instance(
            father_id="c1", alias="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=60,
            peer_balance_mu=6, movements=(
                ledger.Movement(ledger.START, 100), ledger.Movement(ledger.BUILD, -40),
            ), sent_peer_mu=9, mark={},
        )

        self.assertEqual(
            self.sc.settle_stop("alias", "manual_kill", delegated=True, father_id="c1"), 60)

        self.assertEqual(self.rows("alias")[-2]["kind"], ledger.REFUND)
        self.assertEqual(self.rows("alias")[-1]["kind"], ledger.STOP)
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


# How long a writer waits for the lock of the file. The test has three writers on one
# file. On a disk with a high load, the 30 s of a node can be too short for them.
TEST_LOCK_WAIT_SECONDS = 180


def _write_from_another_process(path, account, count):
    """What the CLI does: write to the same file, from a process of its own."""
    connection = sqlite3.connect(path, timeout=TEST_LOCK_WAIT_SECONDS, check_same_thread=False)
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
            connection = sqlite3.connect(path, timeout=TEST_LOCK_WAIT_SECONDS, check_same_thread=False)
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
                    worker.join(timeout=2 * TEST_LOCK_WAIT_SECONDS)
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
            father_id="c1", alias="alias", external_token="peer-token",
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

    def test_a_correction_of_what_the_peer_charged_updates_the_totals_and_not_the_deposit(self):
        # What the peer charged this node is measured, and the instance is charged by
        # the price of this node (#577): a correction moves the totals only.
        self.sc.settle_delegated_tick(
            "peer-token", 30, {"peer_balance_mu": 970, "verified_consumption": 30,
                               "verified_father_net": 0, "charged_delta": 30,
                               "charged_until": 1000.0})
        self.sc.settle_delegated_tick(
            "peer-token", 0, {"peer_balance_mu": 975, "verified_consumption": 25,
                              "charged_delta": -5, "charged_until": 2000.0})

        row = self.sc.get_delegated_instances()[0]
        self.assertEqual((row["verified_consumption_peer_mu"], row["charged_peer_mu"]), (25, 25))
        self.assertEqual(row["charged_until"], 2000.0)
        self.assertEqual(self.sc.get_delegated_balance("peer-token"), 470)
        self.assertEqual(int(self.rows("alias")[-1]["amount_mu"]), -30)
        self.assert_adds_up("alias", 470)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OurAccountOnAPeerTests(_Database):
    """What guards the account that this node holds on a peer (#570)."""

    def setUp(self):
        super().setUp()
        self.sc.add_peer("p", b"")
        self.sc.add_client("c1", 100, None)
        self.sc.add_delegated_instance(
            father_id="c1", alias="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=60,
            peer_balance_mu=6, movements=(ledger.Movement(ledger.START, 60),), sent_peer_mu=9)
        self.ref = ledger.instance_ref("peer-token")

    def test_a_new_client_is_a_mark_at_the_genesis_of_its_account(self):
        self.sc.add_external_client("p", "client-1")

        mark = self.sc.get_peer_statement_mark("p")

        self.assertEqual((mark["seq"], mark["hash"], mark["balance_mu"]),
                         (0, ledger.genesis_hash("client-1"), 0))
        self.assertAlmostEqual(mark["ts"], time.time(), delta=5)

    def test_the_same_client_again_keeps_its_mark(self):
        self.sc.add_external_client("p", "client-1")
        self.sc.set_peer_statement_mark("p", 7, "h", 3, 5)

        self.sc.add_external_client("p", "client-1")

        self.assertEqual(self.sc.get_peer_statement_mark("p")["seq"], 7)

    def test_another_client_starts_again_from_its_genesis_and_its_counters(self):
        self.sc.add_external_client("p", "client-1")
        self.sc.set_peer_statement_mark("p", 7, "h", 3, 5)
        self.sc.add_statement_read("p", rows_priced=4)

        self.sc.add_external_client("p", "client-2")

        self.assertEqual(self.sc.get_peer_statement_mark("p")["hash"], ledger.genesis_hash("client-2"))
        self.assertEqual(self.sc.get_statement_counters("p")["reads"], 0)

    def test_deleting_the_client_clears_the_mark(self):
        self.sc.add_external_client("p", "client-1")

        self.sc.delete_external_client("p")

        self.assertEqual(self.sc.get_peer_statement_mark("p"), {})
        self.assertEqual(self.sc.peers_with_client(), [])

    def test_the_peers_with_a_client(self):
        self.sc.add_external_client("p", "client-1")

        self.assertEqual(self.sc.peers_with_client(), ["p"])

    def test_a_top_up_is_reserved_before_the_peer_is_asked_and_taken_with_the_mark(self):
        first = self.sc.reserve_top_up("alias", 50)
        second = self.sc.reserve_top_up("alias", 20)
        self.assertEqual([(r[0], r[1], r[3]) for r in self.sc.top_up_reservations("p")],
                         [(first, self.ref, 50), (second, self.ref, 20)])

        self.sc.set_peer_statement_mark("p", 1, "h", 0, 5, top_ups_taken={first: 30, second: 20})
        self.assertEqual([(r[0], r[3]) for r in self.sc.top_up_reservations("p")], [(first, 20)])

        self.sc.drop_top_up_reservation(first)
        self.assertEqual(self.sc.top_up_reservations("p"), [])

    def test_a_reservation_of_a_child_that_is_not_known_is_none(self):
        self.assertIsNone(self.sc.reserve_top_up("nobody", 50))

    def test_old_reservations_are_swept(self):
        self.sc.reserve_top_up("alias", 50)
        SQLConnection._connection.execute("UPDATE top_up_reservations SET ts = 0")
        SQLConnection._connection.commit()

        self.assertEqual(self.sc.prune_top_up_reservations(max_age_seconds=3600), 1)

    def test_an_attempt_keeps_what_the_mark_says_of_it_by_its_reference(self):
        self.sc.add_start_attempt("p", "r1", 100)
        self.sc.add_start_attempt("p", "r2", 200)
        self.sc.add_start_attempt("p", "r3", 300)

        self.sc.bind_start_attempt("p", "r1", "token-1")
        self.sc.set_peer_statement_mark("p", 1, "h", 0, 5, attempts={
            "r2": (4, 200, True, ledger.instance_ref("token-2")), "r3": (5, 300, False, "")})

        attempts = self.sc.start_attempts("p")
        self.assertEqual([(a[1], a[3], a[4], a[5], a[6], a[8]) for a in attempts], [
            ("r1", 100, ledger.instance_ref("token-1"), None, 0, False),
            ("r2", 200, ledger.instance_ref("token-2"), 4, 200, True),
            ("r3", 300, "", 5, 300, False)])
        self.assertEqual(self.sc.delegated_starts_on_peer("p")[ledger.instance_ref("token-1")], 100)
        self.assertEqual(self.sc.orphan_starts("p"), ["r3"])

    def test_a_reference_of_ours_with_no_attempt_gets_one_with_the_mark(self):
        self.sc.set_peer_statement_mark("p", 1, "h", 0, 5, attempts={"r1": (4, 70, False, "")})

        self.assertEqual([(a[1], a[3], a[5], a[6]) for a in self.sc.start_attempts("p")],
                         [("r1", None, 4, 70)])
        self.assertEqual(self.sc.orphan_starts("p"), ["r1"])

    def test_the_mark_does_not_forget_a_token_that_came_meanwhile(self):
        self.sc.add_start_attempt("p", "r1", 100)
        self.sc.bind_start_attempt("p", "r1", "token-1")

        self.sc.set_peer_statement_mark("p", 1, "h", 0, 5, attempts={"r1": (4, 100, False, "")})

        self.assertEqual(self.sc.start_attempts("p")[0][4], ledger.instance_ref("token-1"))

    def test_a_stopped_orphan_is_closed_and_named(self):
        self.sc.set_peer_statement_mark("p", 1, "h", 0, 5, attempts={"r1": (4, 70, False, "")})

        self.sc.settle_orphan_start("p", "r1", "token-1")

        self.assertEqual(self.sc.orphan_starts("p"), [])
        self.assertEqual(self.sc.delegated_starts_on_peer("p")[ledger.instance_ref("token-1")], 70)
        self.assertTrue(self.sc.start_attempts("p")[0][8])

    def test_a_refused_attempt_says_when_and_is_no_orphan(self):
        self.sc.add_start_attempt("p", "r1", 100)
        self.sc.set_peer_statement_mark("p", 1, "h", 0, 5, attempts={"r1": (4, 100, False, "")})

        self.sc.refuse_start_attempt("p", "r1")

        self.assertAlmostEqual(self.sc.start_attempts("p")[0][7], time.time(), delta=5)
        self.assertEqual(self.sc.orphan_starts("p"), [])

    def test_the_peers_with_a_start_that_timed_out_are_read(self):
        self.sc.add_start_attempt("new", "r1", 100)
        self.sc.add_start_attempt("old", "r2", 100)
        self.sc.add_start_attempt("bound", "r3", 100)
        self.sc.bind_start_attempt("bound", "r3", "token")
        SQLConnection._connection.execute("UPDATE start_attempts SET ts = 0 WHERE peer_id = 'old'")
        SQLConnection._connection.commit()

        self.assertEqual(self.sc.peers_with_unsettled_starts(since_ts=10), ["new"])
        self.sc.set_peer_statement_mark("old", 1, "h", 0, 5, attempts={"r2": (4, 100, False, "")})
        self.assertEqual(sorted(self.sc.peers_with_unsettled_starts(since_ts=10)), ["new", "old"])

    def test_the_statement_charges_move_with_the_mark(self):
        self.sc.set_peer_statement_mark("p", 1, "h", 0, 5, queries_verified=(2, 30))

        counters = self.sc.get_statement_counters("p")
        self.assertEqual((counters["verified"], counters["verified_mu"]), (2, 30))

    def test_old_attempts_are_swept(self):
        self.sc.add_start_attempt("p", "r1", 100)
        SQLConnection._connection.execute("UPDATE start_attempts SET ts = 0")
        SQLConnection._connection.commit()

        self.assertEqual(self.sc.prune_start_attempts(max_age_days=30), 1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StartReferenceBooksTests(_Database):
    """#578: what a node keeps of the StartReference of each request that it answers."""

    def test_a_reference_is_claimed_once_for_each_caller(self):
        self.assertTrue(self.sc.claim_start_ref("client", "r1"))
        self.assertFalse(self.sc.claim_start_ref("client", "r1"))
        self.assertTrue(self.sc.claim_start_ref("another-client", "r1"))
        self.assertEqual(self.sc.get_start_ref("client", "r1"), ("running", None, None))
        self.assertIsNone(self.sc.get_start_ref("client", "r2"))

    def test_a_start_keeps_its_instance_or_its_error(self):
        self.sc.claim_start_ref("client", "r1")
        self.sc.claim_start_ref("client", "r2")

        self.sc.finish_start_ref("client", "r1", b"instance", "token")
        self.sc.fail_start_ref("client", "r2", "no room")

        self.assertEqual(self.sc.get_start_ref("client", "r1"), ("started", b"instance", None))
        self.assertEqual(self.sc.get_start_ref("client", "r2"), ("failed", None, "no room"))

    def test_a_start_that_ran_when_the_node_stopped_failed(self):
        self.sc.claim_start_ref("client", "r1")

        self.assertEqual(self.sc.fail_running_start_refs(), 1)

        self.assertEqual(self.sc.get_start_ref("client", "r1")[0], "failed")

    def test_a_reference_is_kept_while_its_instance_lives(self):
        for reference in ("lives", "dead", "failed", "running"):
            self.sc.claim_start_ref("client", reference)
        self.sc.finish_start_ref("client", "lives", b"", "instance-that-lives")
        self.sc.finish_start_ref("client", "dead", b"", "instance-that-died")
        self.sc.fail_start_ref("client", "failed", "no room")
        connection = SQLConnection._connection
        connection.execute("INSERT INTO local_instances (id, name) VALUES ('instance-that-lives', 'a')")
        connection.execute("INSERT INTO local_instances (id, name, killed_at) "
                           "VALUES ('instance-that-died', 'b', CURRENT_TIMESTAMP)")
        connection.execute("UPDATE start_refs SET ts = 0")
        connection.commit()

        self.assertEqual(self.sc.prune_start_refs(max_age_days=30), 2)

        self.assertEqual([self.sc.get_start_ref("client", r) is not None
                          for r in ("lives", "dead", "failed", "running")],
                         [True, False, False, True])


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
            father_id="c1", alias="alias", external_token="peer-token",
            peer_id="p", serialized_instance=b"", service_id="s", balance_mu=60,
            peer_balance_mu=6, movements=(ledger.Movement(ledger.START, 60),),
            sent_peer_mu=9, mark={},
        )
        before = len(self.rows("c1"))

        with self.assertRaises(balance_statement.StatementUnavailable):
            balance_statement.answer(caller="c1", token="alias", from_seq=1)
        self.assertEqual(len(self.rows("c1")), before, "nothing is charged for it")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DescendantReadTests(_Database):
    """The holder of a token reads the accounts that the chain of that instance links (#631).

    The tree: client g, its instance p, the child c of p, and the child d of c.
    """

    def setUp(self):
        super().setUp()
        self.mnemonic = Mnemonic("english").generate()
        patcher = patch.object(node_identity, "get_identity_mnemonic", return_value=self.mnemonic)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.peer_id = node_identity.get_node_public_key_hex()
        self.sc.add_client("g", 1000, None)
        self.sc.add_client("other", 1000, None)
        self.add_instance("p", "g", 500, movements=(ledger.Movement(ledger.START, 500),))
        self.child("p", "c", 200)
        self.child("c", "d", 50)

    def child(self, father, token, mu):
        seq = self.sc.move_balance(father, -mu, ledger.CHILD_START)
        self.add_instance(token, father, mu, movements=(ledger.Movement(ledger.START, mu),))
        self.sc.bind_child(father, seq, token)

    def read(self, caller="g", token="p", ref="", **kwargs):
        return balance_statement.answer(
            caller=caller, token=token, instance_ref=ref, from_seq=1, **kwargs)

    def test_the_holder_of_a_token_reads_a_child_by_its_ref(self):
        statement = self.read(ref=ledger.instance_ref("c"))

        self.assertEqual(statement.account_ref, ledger.account_ref("c"))
        self.assertEqual(statement.balance.n, "150")
        self.assertTrue(balance_statement.verify(statement, self.peer_id))

    def test_the_read_goes_down_more_than_one_level(self):
        statement = self.read(ref=ledger.instance_ref("d"))

        self.assertEqual(statement.account_ref, ledger.account_ref("d"))
        self.assertEqual(statement.balance.n, "50")

    def test_an_instance_reads_its_own_descendants_with_no_token(self):
        statement = balance_statement.answer(
            caller="p", instance_ref=ledger.instance_ref("d"), from_seq=1)

        self.assertEqual(statement.account_ref, ledger.account_ref("d"))

    def test_the_parent_dies_and_a_grandparent_reads_the_stop_of_the_child(self):
        self.sc.settle_stop("p", "manual_kill", father_id="g")
        self.sc.settle_stop("c", "process_lost", father_id="p", credit=False)

        statement = self.read(ref=ledger.instance_ref("c"))

        stop = statement.movements[-1]
        self.assertEqual(stop.kind, ledger.STOP)
        self.assertEqual(json.loads(stop.detail),
                         {"leftover": "150", "reason": "process_lost"})
        self.assertEqual(statement.balance.n, "0")
        self.assertTrue(balance_statement.verify(statement, self.peer_id))
        # The chain of the dead child still adds up from its first row.
        self.assert_chained("c")

    def test_a_reader_cannot_use_the_token_of_an_instance_that_it_did_not_start(self):
        with self.assertRaises(balance_statement.StatementRefused):
            self.read(caller="other", ref=ledger.instance_ref("c"))

    def test_a_ref_that_no_chain_links_is_refused(self):
        self.add_instance("loose", "g", 10, movements=(ledger.Movement(ledger.START, 10),))
        with self.assertRaises(balance_statement.StatementRefused):
            self.read(ref=ledger.instance_ref("loose"))
        with self.assertRaises(balance_statement.StatementRefused):
            self.read(ref=ledger.instance_ref("nobody"))

    def test_a_child_of_another_branch_is_not_reachable_from_a_sibling(self):
        self.add_instance("q", "g", 100, movements=(ledger.Movement(ledger.START, 100),))
        with self.assertRaises(balance_statement.StatementRefused):
            self.read(token="q", ref=ledger.instance_ref("c"))

    def test_a_loop_in_the_links_ends(self):
        seq = self.sc.move_balance("c", -1, ledger.CHILD_START)
        self.sc.bind_child("c", seq, "p")
        with self.assertRaises(balance_statement.StatementRefused):
            self.read(ref=ledger.instance_ref("nobody"))

    def test_the_read_stops_at_the_depth_limit(self):
        with patch.object(ledger, "max_descendant_depth", return_value=1):
            self.assertTrue(self.read(ref=ledger.instance_ref("c")))
            with self.assertRaises(balance_statement.StatementRefused):
                self.read(ref=ledger.instance_ref("d"))

    def test_the_read_stops_at_the_account_limit(self):
        with patch.object(ledger, "max_descendant_accounts", return_value=1):
            with self.assertRaises(balance_statement.StatementRefused):
                self.read(ref=ledger.instance_ref("d"))

    def test_the_caller_pays_and_the_charge_names_the_descendant_by_its_ref(self):
        before = self.sc.get_balance_by_client_id("g")

        self.read(ref=ledger.instance_ref("c"))

        charge = self.rows("g")[-1]
        self.assertEqual(charge["kind"], ledger.STATEMENT_QUERY)
        self.assertEqual(charge["instance_ref"], ledger.instance_ref("c"))
        self.assertLess(self.sc.get_balance_by_client_id("g"), before)

    def test_no_statement_holds_a_token(self):
        statement = self.read(ref=ledger.instance_ref("d"))
        self.assertNotIn("d", [statement.account_ref])
        for movement in statement.movements:
            self.assertNotIn("p", movement.instance_ref.split())

    def test_a_delegated_child_is_asked_with_the_token_of_this_node(self):
        self.sc.add_delegated_instance(
            father_id="p", alias="alias", external_token="peer-token",
            peer_id="peer-1", serialized_instance=b"", service_id="s", balance_mu=10,
            peer_balance_mu=1, movements=(ledger.Movement(ledger.START, 10),),
            sent_peer_mu=0, mark=None,
        )
        seq = self.sc.move_balance("p", -10, ledger.CHILD_START)
        self.sc.bind_child("p", seq, "alias")
        answer = balance_statement.celaut_pb2.BalanceStatement(head_seq=3)
        asked = []

        def read(peer, client, **kwargs):
            asked.append((peer, client, kwargs))
            return answer

        with patch("src.manager.statement_check.read", side_effect=read), \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="me-there"):
            statement = self.read(ref=ledger.instance_ref("far"), to_seq=5)

        self.assert_relayed(statement, answer)
        self.assertEqual(asked[0][:2], ("peer-1", "me-there"))
        self.assertEqual(asked[0][2]["token"], "peer-token")
        self.assertEqual(asked[0][2]["instance_ref"], ledger.instance_ref("far"))
        self.assertEqual(self.rows("g")[-1]["kind"], ledger.STATEMENT_QUERY)

    def test_a_peer_that_has_no_such_descendant_gives_a_refusal(self):
        self.sc.add_delegated_instance(
            father_id="p", alias="alias", external_token="peer-token",
            peer_id="peer-1", serialized_instance=b"", service_id="s", balance_mu=10,
            peer_balance_mu=1, movements=(ledger.Movement(ledger.START, 10),),
            sent_peer_mu=0, mark=None,
        )
        seq = self.sc.move_balance("p", -10, ledger.CHILD_START)
        self.sc.bind_child("p", seq, "alias")

        class Denied(Exception):
            def code(self):
                import grpc
                return grpc.StatusCode.PERMISSION_DENIED

        with patch("src.manager.statement_check.read", side_effect=Denied()), \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="me-there"):
            with self.assertRaises(balance_statement.StatementRefused):
                self.read(ref=ledger.instance_ref("far"), to_seq=5)

    def delegate(self, mu=10, alias="alias", father="p"):
        self.sc.add_delegated_instance(
            father_id=father, alias=alias,
            external_token="peer-token" if alias == "alias" else f"peer-token-{alias}",
            peer_id="peer-1", serialized_instance=b"", service_id="s", balance_mu=mu,
            peer_balance_mu=1, movements=(ledger.Movement(ledger.START, mu),),
            sent_peer_mu=0, mark=None,
        )
        seq = self.sc.move_balance(father, -mu, ledger.CHILD_START)
        self.sc.bind_child(father, seq, alias)

    def test_the_caller_pays_the_most_rows_before_the_peer_is_asked(self):
        most = balance_statement.statement_price_mu(5)
        self.sc.add_client("poor", balance_statement.statement_price_mu(0), None)
        self.add_instance("pp", "poor", 100, movements=(ledger.Movement(ledger.START, 100),))
        self.delegate(father="pp")
        asked = []
        with patch("src.manager.statement_check.read", side_effect=asked.append), \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            with self.assertRaises(ledger.InsufficientBalance):
                balance_statement.answer(caller="poor", token="pp",
                                         instance_ref=ledger.instance_ref("far"),
                                         from_seq=1, to_seq=5)
        self.assertGreater(most, balance_statement.statement_price_mu(0))
        self.assertEqual(asked, [])
        self.assertEqual(self.sc.get_balance_by_client_id("poor"),
                         balance_statement.statement_price_mu(0))

    def test_the_error_of_a_peer_does_not_reach_the_caller(self):
        self.delegate()
        with patch("src.manager.statement_check.read",
                   side_effect=RuntimeError("peer 10.1.2.3 is down")), \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            with self.assertRaises(balance_statement.StatementUnavailable) as raised:
                self.read(ref=ledger.instance_ref("far"), to_seq=5)
        self.assertNotIn("10.1.2.3", str(raised.exception))
        self.assertNotIn("peer-1", str(raised.exception))

    def test_a_stopped_delegated_parent_is_still_asked(self):
        self.delegate()
        self.sc.settle_stop("alias", "manual_kill", delegated=True, father_id="p")
        answer = balance_statement.celaut_pb2.BalanceStatement(head_seq=3)
        with patch("src.manager.statement_check.read", return_value=answer) as read, \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            # The root of the read is the stopped delegated instance itself.
            statement = balance_statement.answer(
                caller="p", token="alias", instance_ref=ledger.instance_ref("far"),
                from_seq=1, to_seq=5)
        self.assert_relayed(statement, answer)
        self.assertEqual(read.call_args.kwargs["token"], "peer-token")

    def test_the_hop_budget_ends_the_read_before_a_peer_is_asked(self):
        self.delegate()
        with patch("src.manager.statement_check.read") as read:
            with self.assertRaises(balance_statement.StatementUnavailable):
                self.read(ref=ledger.instance_ref("far"), to_seq=5, remaining_hops=1)
            with self.assertRaises(balance_statement.StatementUnavailable):
                self.read(ref=ledger.instance_ref("far"), to_seq=5, remaining_hops=0)
        read.assert_not_called()

    def test_a_hop_sends_one_hop_less(self):
        self.delegate()
        answer = balance_statement.celaut_pb2.BalanceStatement(head_seq=3)
        with patch("src.manager.statement_check.read", return_value=answer) as read, \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            self.read(ref=ledger.instance_ref("far"), to_seq=5, remaining_hops=3)
        self.assertEqual(read.call_args.kwargs["remaining_hops"], 2)

    def assert_relayed(self, statement, answer):
        """The node gives the statement of the peer as relayed, signed with its own key."""
        self.assertEqual(statement.head_seq, answer.head_seq)
        self.assertTrue(statement.relayed)
        self.assertTrue(balance_statement.verify(statement, self.peer_id))

    def stop_statement(self, leftover, ts, lost=True, reason="manual_kill", period=0):
        """A statement of a descendant that ends with its refund and its stop row."""
        statement = balance_statement.celaut_pb2.BalanceStatement(head_seq=3)
        seq = 1
        if period:
            statement.movements.add(
                seq=seq, ts=ts - 60, kind=ledger.MAINTENANCE, amount={"n": str(-period)},
                balance_after={"n": str(leftover)}, detail='{"mem_limit":1}')
            seq += 1
        statement.movements.add(
            seq=seq, ts=ts, kind=ledger.REFUND, amount={"n": str(-leftover)},
            balance_after={"n": "0"},
            detail=json.dumps({"lost": "father_gone"} if lost else {}) if lost else "")
        statement.movements.add(
            seq=seq + 1, ts=ts, kind=ledger.STOP, amount={"n": "0"}, balance_after={"n": "0"},
            detail=json.dumps({"reason": reason, "leftover": str(leftover)}))
        return statement

    def scored(self, statement, ref="far"):
        """The reasons that one read of ``statement`` through the peer scores."""
        with patch("src.manager.statement_check.read", return_value=statement), \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"), \
                patch("src.reputation_system.interface.update_peer_reputation") as score:
            self.read(ref=ledger.instance_ref(ref), to_seq=5)
        return [(c.kwargs["peer_id"], c.kwargs["reason"]) for c in score.call_args_list]

    def test_a_peer_that_kills_an_orphan_descendant_with_mu_is_scored_once(self):
        self.delegate()
        statement = self.stop_statement(40, int(time.time()))
        self.assertEqual(self.scored(statement), [("peer-1", "killed_with_mu")])
        self.assertEqual(self.scored(statement), [])

    def test_a_stop_whose_father_got_the_mu_is_not_scored(self):
        # A father that stops its child gets the leftover: an honest stop (#631 review).
        self.delegate()
        self.assertEqual(self.scored(self.stop_statement(40, int(time.time()), lost=False)), [])

    def test_a_stop_with_no_mu_is_not_scored(self):
        self.delegate()
        self.assertEqual(self.scored(self.stop_statement(0, int(time.time()))), [])

    def test_a_stop_for_a_fault_of_the_machine_is_not_scored(self):
        self.delegate()
        for reason in ("process_lost", "guest_panic", "activity_window"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.scored(self.stop_statement(40, int(time.time()), reason=reason)), [])

    def test_out_of_balance_is_scored_only_with_a_full_period_left(self):
        self.delegate()
        now = int(time.time())
        self.assertEqual(self.scored(self.stop_statement(
            5, now, reason="out_of_balance", period=10)), [])
        self.assertEqual(self.scored(self.stop_statement(
            5, now, reason="out_of_balance")), [], "no period in the statement: not judged")
        self.assertEqual(self.scored(self.stop_statement(
            30, now, reason="out_of_balance", period=10)), [("peer-1", "killed_with_mu")])

    def test_a_relayed_statement_is_judged_too(self):
        # The peer sets ``relayed`` and signs it: this node cannot know that it is true.
        # A peer must not escape the judgement with one flag.
        self.delegate()
        statement = self.stop_statement(40, int(time.time()))
        statement.relayed = True
        self.assertEqual(self.scored(statement), [("peer-1", "killed_with_mu")])

    def test_a_relayed_statement_is_signed_again_by_this_node(self):
        # A read across two delegations: the near node checks the key of the node that
        # it asked, not the key of the far node (#631 review).
        far = balance_statement.celaut_pb2.BalanceStatement(head_seq=7, signature="ab")
        relayed = balance_statement._relay(far)
        self.assertTrue(relayed.relayed)
        self.assertTrue(balance_statement.verify(relayed, self.peer_id))
        self.assertFalse(far.relayed, "the statement of the peer does not change")
        stripped = balance_statement.celaut_pb2.BalanceStatement()
        stripped.CopyFrom(relayed)
        stripped.relayed = False
        self.assertFalse(balance_statement.verify(stripped, self.peer_id))

    def test_the_digest_of_a_statement_that_is_not_relayed_does_not_change(self):
        statement = balance_statement.celaut_pb2.BalanceStatement(account_ref="a", head_seq=2)
        before = balance_statement.statement_digest(statement)
        statement.relayed = True
        self.assertNotEqual(balance_statement.statement_digest(statement), before)
        statement.relayed = False
        self.assertEqual(balance_statement.statement_digest(statement), before)

    def set_service(self, token, service_id, table="local_instances"):
        SQLConnection._connection.execute(
            f"UPDATE {table} SET service_id = ? WHERE id = ?", (service_id, token))
        SQLConnection._connection.commit()

    def test_the_statement_of_a_descendant_gives_its_service(self):
        self.set_service("d", "AB12")
        statement = self.read(ref=ledger.instance_ref("d"))
        self.assertEqual(statement.service_id, "ab12")
        self.assertTrue(balance_statement.verify(statement, self.peer_id))

    def test_the_signature_covers_the_service(self):
        self.set_service("d", "ab12")
        statement = self.read(ref=ledger.instance_ref("d"))
        statement.service_id = "cd34"
        self.assertFalse(balance_statement.verify(statement, self.peer_id))

    def test_a_stopped_descendant_below_a_stopped_parent_gives_its_service(self):
        self.set_service("c", "ab12")
        self.sc.settle_stop("p", "manual_kill", father_id="g")
        self.sc.settle_stop("c", "process_lost", father_id="p", credit=False)
        statement = self.read(ref=ledger.instance_ref("c"))
        self.assertEqual(statement.service_id, "ab12")

    def test_a_read_without_instance_ref_gives_no_service(self):
        self.set_service("c", "ab12")
        statement = balance_statement.answer(caller="p", token="c", from_seq=1)
        self.assertEqual(statement.service_id, "")

    def test_a_service_id_that_is_not_hex_is_not_given(self):
        self.set_service("d", "not-hex")
        self.assertEqual(self.read(ref=ledger.instance_ref("d")).service_id, "")

    def test_the_digest_without_a_service_does_not_change(self):
        statement = balance_statement.celaut_pb2.BalanceStatement(account_ref="a", head_seq=2)
        before = balance_statement.statement_digest(statement)
        statement.service_id = "ab"
        self.assertNotEqual(balance_statement.statement_digest(statement), before)
        statement.service_id = ""
        self.assertEqual(balance_statement.statement_digest(statement), before)

    def test_a_delegated_descendant_gets_the_service_of_the_peer_or_of_this_node(self):
        self.delegate()
        self.set_service("alias", "ab12", table="delegated_instances")
        for given, expected in (("", "ab12"), ("cd34", "cd34")):
            answer = balance_statement.celaut_pb2.BalanceStatement(head_seq=3,
                                                                  service_id=given)
            with self.subTest(given=given), \
                    patch("src.manager.statement_check.read", return_value=answer), \
                    patch("src.manager.manager.get_client_id_on_other_peer",
                          return_value="x"):
                statement = self.read(ref=ledger.instance_ref("alias"), to_seq=5)
            self.assertEqual(statement.service_id, expected)
            self.assert_relayed(statement, answer)

    def test_a_delegated_descendant_is_asked_by_the_ref_of_the_peer_token(self):
        # Also when it stopped: the peer still holds its chain and its stop row.
        self.delegate()
        self.sc.settle_stop("alias", "peer_stopped", delegated=True, father_id="p")
        answer = balance_statement.celaut_pb2.BalanceStatement(head_seq=3)
        with patch("src.manager.statement_check.read", return_value=answer) as read, \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            statement = self.read(ref=ledger.instance_ref("alias"), to_seq=5)
        self.assert_relayed(statement, answer)
        self.assertEqual(read.call_args.kwargs["token"], "peer-token")
        self.assertEqual(read.call_args.kwargs["instance_ref"], ledger.instance_ref("peer-token"))

    def test_the_error_of_a_peer_does_not_reach_the_caller_for_a_delegated_descendant(self):
        self.delegate()
        for error in (RuntimeError("peer-1 at 10.1.2.3 is down"),
                      balance_statement.StatementInvalid("peer-1 did not sign")):
            with self.subTest(error=type(error).__name__), \
                    patch("src.manager.statement_check.read", side_effect=error), \
                    patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
                with self.assertRaises(balance_statement.StatementUnavailable) as raised:
                    self.read(ref=ledger.instance_ref("alias"), to_seq=5)
                self.assertNotIn("10.1.2.3", str(raised.exception))
                self.assertNotIn("peer-1", str(raised.exception))

    def test_no_account_on_the_peer_names_no_peer_and_charges_nothing(self):
        self.delegate()
        before = len(self.rows("g"))
        with patch("src.manager.statement_check.read") as read, \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value=None):
            with self.assertRaises(balance_statement.StatementUnavailable) as raised:
                self.read(ref=ledger.instance_ref("far"), to_seq=5)
        read.assert_not_called()
        self.assertNotIn("peer", str(raised.exception))
        self.assertEqual(len(self.rows("g")), before)

    def test_a_read_that_asks_many_peers_is_charged_once(self):
        self.delegate(alias="a1")
        self.delegate(alias="a2")
        self.delegate(alias="a3")
        before = len(self.rows("g"))

        class Denied(Exception):
            def code(self):
                import grpc
                return grpc.StatusCode.PERMISSION_DENIED

        with patch("src.manager.statement_check.read", side_effect=Denied()) as read, \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            with self.assertRaises(balance_statement.StatementRefused):
                self.read(ref=ledger.instance_ref("far"), to_seq=5, remaining_hops=8)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(len(self.rows("g")) - before, 1)

    def test_the_hop_budget_is_shared_between_the_peers(self):
        # A read never reaches more nodes than its budget (#631 review: fan-out).
        for alias in ("a1", "a2", "a3"):
            self.delegate(alias=alias)
        with patch("src.manager.statement_check.read",
                   side_effect=balance_statement.StatementUnavailable("x")) as read, \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            with self.assertRaises(balance_statement.StatementUnavailable):
                self.read(ref=ledger.instance_ref("far"), to_seq=5, remaining_hops=7)
        self.assertEqual([c.kwargs["remaining_hops"] for c in read.call_args_list], [2, 2, 2])
        with patch("src.manager.statement_check.read",
                   side_effect=balance_statement.StatementUnavailable("x")) as read, \
                patch("src.manager.manager.get_client_id_on_other_peer", return_value="x"):
            with self.assertRaises(balance_statement.StatementUnavailable):
                self.read(ref=ledger.instance_ref("far"), to_seq=5, remaining_hops=3)
        self.assertEqual([c.kwargs["remaining_hops"] for c in read.call_args_list], [1, 1])

    def test_split_hops(self):
        self.assertEqual(balance_statement.split_hops(1, 3), [])
        self.assertEqual(balance_statement.split_hops(4, 1), [3])
        self.assertEqual(balance_statement.split_hops(4, 2), [1, 1])
        self.assertEqual(balance_statement.split_hops(4, 10), [1, 1, 1])
        self.assertEqual(balance_statement.split_hops(9, 0), [])

    def test_an_account_that_goes_away_does_not_give_its_token_to_the_caller(self):
        with patch.object(balance_statement, "_answer_account",
                          side_effect=ledger.NoSuchAccount("c")):
            with self.assertRaises(balance_statement.StatementRefused) as raised:
                self.read(ref=ledger.instance_ref("c"))
        self.assertNotIn("'c'", str(raised.exception))
        self.assertNotIsInstance(raised.exception, ledger.NoSuchAccount)

    def test_the_instance_ref_of_the_token_itself_reads_that_account(self):
        self.sc.settle_stop("c", "manual_kill", father_id="p")
        statement = balance_statement.answer(
            caller="p", token="c", instance_ref=ledger.instance_ref("c"), from_seq=1)
        self.assertEqual(statement.account_ref, ledger.account_ref("c"))
        self.assertEqual(statement.movements[-1].kind, ledger.STOP)

    def test_the_link_names_the_service_of_the_child(self):
        seq = self.sc.move_balance("p", -5, ledger.CHILD_START)
        self.add_instance("e", "p", 5, movements=(ledger.Movement(ledger.START, 5),))
        self.sc.bind_child("p", seq, "e", service_id="ab12")

        statement = self.read(ref=ledger.instance_ref("p"))
        links = [json.loads(m.detail) for m in statement.movements
                 if m.kind == ledger.CHILD_BOUND and m.instance_ref == ledger.instance_ref("e")]
        self.assertEqual(links, [{"starts_seq": str(seq), "service_id": "ab12"}])

    def maintenance_statement(self, ts, mem_limit=1, relayed=False, seq=4, seconds=None,
                              stop_ts=None):
        statement = balance_statement.celaut_pb2.BalanceStatement(head_seq=seq, relayed=relayed)
        detail = {"mem_limit": mem_limit}
        if seconds is not None:
            detail["seconds"] = seconds
        if stop_ts is not None:
            statement.movements.add(
                seq=seq - 1, ts=stop_ts, kind=ledger.STOP, amount={"n": "0"},
                balance_after={"n": "10"},
                detail=json.dumps({"reason": "manual_kill", "leftover": "0"}))
        statement.movements.add(
            seq=seq, ts=ts, kind=ledger.MAINTENANCE, amount={"n": "-3"},
            balance_after={"n": "7"}, detail=json.dumps(detail))
        return statement

    def test_maintenance_after_a_tunnel_that_failed_is_scored(self):
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 1000)
        statement = self.maintenance_statement(now)
        self.assertEqual(self.scored(statement, ref="alias"), [],
                         "one failure is not enough: the instance can answer again")
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now + 1000)
        self.assertEqual(self.scored(statement, ref="alias"),
                         [("peer-1", "maintenance_while_unreachable")])
        self.assertEqual(self.scored(statement, ref="alias"), [], "once for each descendant")

    def test_maintenance_after_the_instance_answers_again_is_not_scored(self):
        # A hibernated instance that its father wakes: the failure is old, a tunnel works
        # later, and the maintenance with memory is honest.
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 3000)
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 2900)
        self.assertEqual(self.scored(self.maintenance_statement(now), ref="alias"), [],
                         "the period starts after the last failure")

    def test_maintenance_is_not_scored_without_a_failure_or_after_a_tunnel_that_worked(self):
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        statement = self.maintenance_statement(now)
        self.assertEqual(self.scored(statement, ref="alias"), [])
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 1000)
        self.sc.record_descendant_tunnel("peer-1", ref, ok=True, ts=now - 500)
        self.assertEqual(self.scored(statement, ref="alias"), [])

    def test_maintenance_close_to_the_failure_or_of_the_disk_only_is_not_scored(self):
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 30)
        self.assertEqual(self.scored(self.maintenance_statement(now), ref="alias"), [])
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 1000)
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now + 1000)
        self.assertEqual(self.scored(self.maintenance_statement(now, mem_limit=0),
                                     ref="alias"), [])

    def test_a_relayed_maintenance_statement_is_judged_too(self):
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 1000)
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now + 1000)
        self.assertEqual(self.scored(self.maintenance_statement(now, relayed=True),
                                     ref="alias"),
                         [("peer-1", "maintenance_while_unreachable")])

    def test_maintenance_while_unreachable_is_scored_once_for_each_descendant(self):
        # Later rows of the same dead VM do not cost the peer a second fault.
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 5000)
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now + 1000)
        self.assertEqual(self.scored(self.maintenance_statement(now - 1000, seq=4),
                                     ref="alias"),
                         [("peer-1", "maintenance_while_unreachable")])
        self.assertEqual(self.scored(self.maintenance_statement(now, seq=9), ref="alias"), [])
        faults = self.sc._execute(
            "SELECT account_ref, reason FROM statement_faults").fetchall()
        self.assertEqual([(f["account_ref"], f["reason"]) for f in faults],
                         [(ledger.account_ref(ref), "maintenance_while_unreachable")])
        self.assertNotIn("peer-token", json.dumps([dict(f) for f in faults]))

    def test_the_period_in_which_the_vm_stopped_is_not_scored(self):
        # An honest node can charge the period that holds the failure, then stop.
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 1000)
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now + 1000)
        self.assertEqual(self.scored(self.maintenance_statement(now, seconds=1200),
                                     ref="alias"), [])
        self.assertEqual(self.scored(self.maintenance_statement(now, seconds=600),
                                     ref="alias"),
                         [("peer-1", "maintenance_while_unreachable")])

    def test_maintenance_after_a_stop_row_is_not_scored(self):
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=now - 1000)
        statement = self.maintenance_statement(now, stop_ts=now - 500)
        self.assertNotIn(("peer-1", "maintenance_while_unreachable"),
                         self.scored(statement, ref="alias"))

    def test_maintenance_on_another_peer_or_descendant_is_not_scored(self):
        self.delegate()
        now = int(time.time())
        self.sc.record_descendant_tunnel("peer-2", ledger.instance_ref("peer-token"),
                                         ok=False, ts=now - 1000)
        self.sc.record_descendant_tunnel("peer-1", ledger.instance_ref("other"),
                                         ok=False, ts=now - 1000)
        self.assertEqual(self.scored(self.maintenance_statement(now), ref="alias"), [])

if __name__ == "__main__":
    unittest.main()
