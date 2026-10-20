"""The ledger of balance movements and GetBalanceStatement (#570).

Every write of a balance goes with its row, in one transaction, so the rows of an
account add up to its balance. A statement is charged to whoever asks, and is signed.
"""

import sqlite3
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
        self.assertEqual(self.rows("c1")[-1]["instance_token"], "child")

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
        self.assertEqual(self.rows("c1")[-1]["instance_token"], "i1")
        self.assertIsNone(self.rows("i1")[-1]["instance_token"])
        self.assert_adds_up("c1", 40)
        self.assert_adds_up("i1", 110)

    def test_the_token_of_a_child_start_is_set_once(self):
        self.sc.add_client("c1", 100, None)
        seq = self.sc.move_balance("c1", -10, ledger.CHILD_START)

        self.assertTrue(self.sc.set_movement_token("c1", seq, "child-1"))
        self.assertFalse(self.sc.set_movement_token("c1", seq, "child-2"))
        self.assertEqual(self.rows("c1")[-1]["instance_token"], "child-1")

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
        self.assertEqual(self.sc.delegated_starts_on_peer("p"), {"peer-token": 9})

    def test_the_sweep_keeps_the_last_row_of_a_live_account(self):
        self.sc.add_client("c1", 100, None)
        self.sc.move_balance("c1", 5, ledger.DEPOSIT)
        SQLConnection._connection.execute("UPDATE balance_movements SET ts = 0")
        SQLConnection._connection.commit()

        self.assertEqual(self.sc.prune_balance_movements(max_age_days=30), 1)
        self.sc.move_balance("c1", 5, ledger.DEPOSIT)

        self.assertEqual([r["seq"] for r in self.rows("c1")], [2, 3])


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

    def test_the_charge_is_the_last_movement_and_the_statement_adds_up(self):
        read = self.read()

        last = read["movements"][-1]
        self.assertEqual(last["kind"], ledger.STATEMENT_QUERY)
        # Six rows before it and itself: 1 + 7.
        self.assertEqual(int(last["amount_mu"]), -8)
        self.assertEqual(int(last["balance_after_mu"]), read["balance_mu"])
        self.assert_adds_up("c1", read["balance_mu"])

    def test_last_counts_the_movements_before_the_charge(self):
        read = self.read(last=2)

        self.assertEqual([m["seq"] for m in read["movements"]], [5, 6, 7])
        self.assertFalse(read["truncated"])

    def test_since_keeps_only_the_span(self):
        SQLConnection._connection.execute(
            "UPDATE balance_movements SET ts = ? WHERE seq < 4", (int(time.time()) - 3600,)
        )
        SQLConnection._connection.commit()

        read = self.read(since_seconds=60)

        self.assertEqual([m["seq"] for m in read["movements"]], [4, 5, 6, 7])

    def test_the_node_cap_cuts_the_answer_and_says_so(self):
        read = self.read(max_rows=3)

        self.assertEqual([m["seq"] for m in read["movements"]], [5, 6, 7])
        self.assertTrue(read["truncated"])

    def test_a_father_reads_its_child_and_pays_for_it(self):
        self.add_instance("i1", "c1", 70)

        read = self.read(account="i1", payer="c1")

        self.assertEqual([m["kind"] for m in read["movements"]], [ledger.START])
        self.assertEqual(read["balance_mu"], 70)
        charge = self.rows("c1")[-1]
        self.assertEqual((charge["kind"], charge["instance_token"]), (ledger.STATEMENT_QUERY, "i1"))
        self.assertEqual(int(charge["amount_mu"]), -2)

    def test_a_payer_that_cannot_pay_gets_nothing(self):
        with self.assertRaises(ledger.InsufficientBalance):
            self.read(price=lambda rows: 10 ** 9, allow_debt=False)
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
        self.add_instance("i1", "c1", 1000)
        self.add_instance("i2", "someone-else", 1000)

    def test_the_answer_verifies_against_the_key_of_the_node(self):
        statement = balance_statement.answer(caller="c1")

        self.assertEqual(statement.account, "c1")
        self.assertTrue(balance_statement.verify(statement, self.peer_id))

    def test_a_changed_movement_breaks_the_signature(self):
        statement = balance_statement.answer(caller="c1")
        statement.movements[0].amount.n = "999999"

        self.assertFalse(balance_statement.verify(statement, self.peer_id))

    def test_a_caller_reads_its_child_by_token(self):
        statement = balance_statement.answer(caller="c1", token="i1")

        self.assertEqual(statement.account, "i1")
        self.assertTrue(balance_statement.verify(statement, self.peer_id))

    def test_a_caller_cannot_read_an_account_that_is_not_its_own_or_a_child(self):
        with self.assertRaises(balance_statement.StatementRefused):
            balance_statement.answer(caller="c1", token="i2")

    def test_a_child_reads_its_own_account_and_not_its_father(self):
        self.assertEqual(balance_statement.answer(caller="i1").account, "i1")
        with self.assertRaises(balance_statement.StatementRefused):
            balance_statement.answer(caller="i1", token="c1")


if __name__ == "__main__":
    unittest.main()
