"""Regression tests for the ``delegated_instances`` accessors.

These queries had drifted from their own schema: they addressed a ``token``
column and a ``serialized_service`` column, neither of which exists (the key is
``token_delegation``, the payload is ``serialized_instance``). Every read and the
delete were failing, which is why stopping a delegated instance always died in
``stop_instance``'s except branch — silently, refunding nothing.

The schema here comes from ``migrate.create_tables`` rather than a copy of the
DDL, so the tests fail again if the table and its accessors ever diverge.
"""

import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedInstanceQueryTests(unittest.TestCase):
    PEER_TOKEN = "token-as-the-peer-knows-it"
    HASHED = "our-hashed-alias"  # any value: the alias is only a key here

    def setUp(self):
        self.sc = SQLConnection()
        self._original_connection = SQLConnection._connection

        # Swap in an in-memory database carrying the real schema.
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection

        self.sc.add_delegated_instance(
            father_id="father-instance",
            alias=self.HASHED,
            external_token=self.PEER_TOKEN,
            peer_id="peer-1",
            serialized_instance=b"original-instance",
            service_id="service-1",
            balance_mu=900,
            peer_balance_mu=1800,
        )

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original_connection

    def test_the_stored_instance_can_be_read_back(self):
        self.assertEqual(
            self.sc.get_delegated_instance(token=self.PEER_TOKEN), b"original-instance"
        )

    def test_the_father_can_be_read_back(self):
        self.assertEqual(
            self.sc.get_external_father_id(token=self.PEER_TOKEN), "father-instance"
        )

    def test_the_peer_token_resolves_from_our_hashed_alias(self):
        self.assertEqual(
            self.sc.get_delegated_token_by_id(id=self.HASHED), self.PEER_TOKEN
        )

    def test_the_peer_resolves_from_the_token(self):
        self.assertEqual(
            self.sc.get_peer_id_by_external_service(token=self.PEER_TOKEN), "peer-1"
        )

    def test_every_delegated_instance_can_be_listed_for_startup_restore(self):
        rows = self.sc.get_delegated_instances()

        self.assertEqual(len(rows), 1)
        # The time that the child is charged up to starts at the start (#577).
        self.assertIsInstance(rows[0].pop('charged_until'), float)
        self.assertEqual(
            rows[0],
            {
                'token': self.PEER_TOKEN,
                'id': self.HASHED,
                'peer_id': 'peer-1',
                'father_id': 'father-instance',
                'serialized_instance': b'original-instance',
                'balance_mu': 900,
                'peer_balance_mu': 1800,
                'peer_statement_seq': None,
                'peer_statement_ts': None,
                'peer_statement_hash': None,
                'verified_consumption_peer_mu': 0,
                'verified_father_net_peer_mu': 0,
                'father_net_peer_mu': 0,
                'charged_peer_mu': 0,
                # No shape was given: the child is charged nothing (#577).
                'mem_limit': None,
                'disk_space': None,
                'cpu_period': None,
                'cpu_quota': None,
                'arch': None,
                'launch_cpu_bp': None,
                'launch_mem_bp': None,
                'launch_disk_bp': None,
                'max_mem_limit': None,
                'max_disk_space': None,
                'max_cpu_period': None,
                'max_cpu_quota': None,
                'base_mem_limit': None,
                'base_disk_space': None,
                'base_cpu_period': None,
                'base_cpu_quota': None,
                'peer_base_mem_limit': None,
                'peer_base_disk_space': None,
                'peer_base_cpu_period': None,
                'peer_base_cpu_quota': None,
                # No quote was given: the charge of the peer is not checked (#567).
                'quote_init_maintenance_cost': None,
                'quote_max_maintenance_cost': None,
                'quote_maintenance_seconds_loop': None,
                'quote_variance': None,
            },
        )

    def test_a_charge_writes_the_deposit_and_its_mark_together(self):
        """One transaction, so no failure can leave one of them behind.

        The tick charges what the peer metered since the mark, so a deposit written
        without its mark bills the same consumption again next tick. The peer's
        figure stays on its own scale: the tick sums before converting, so a rate
        that moved between two readings must not read as consumption.
        """
        self.assertEqual(self.sc.get_delegated_balance(token=self.PEER_TOKEN), 900)

        self.sc.settle_delegated_tick(
            token=self.PEER_TOKEN, charge_mu=200,
            mark={"peer_balance_mu": 1400, "seq": 7, "ts": 70, "hash": "abc"},
        )

        self.assertEqual(self.sc.get_delegated_balance(token=self.PEER_TOKEN), 700)
        row = self.sc.get_delegated_instances()[0]
        self.assertEqual(
            (row['peer_balance_mu'], row['peer_statement_seq'], row['peer_statement_ts'],
             row['peer_statement_hash']),
            (1400, 7, 70, "abc"),
        )

    def test_a_tick_with_nothing_to_charge_only_moves_the_mark(self):
        self.sc.settle_delegated_tick(
            token=self.PEER_TOKEN, charge_mu=0, mark={"peer_balance_mu": 1500},
        )

        self.assertEqual(self.sc.get_delegated_balance(token=self.PEER_TOKEN), 900)
        self.assertEqual(self.sc.get_delegated_instances()[0]['peer_balance_mu'], 1500)

    def test_a_row_with_no_figures_reads_as_zero(self):
        """A NULL column and a token that is simply gone.

        Both are "nothing here", and zero is the only reading that cannot hand a
        father MU nobody paid in.
        """
        SQLConnection._connection.execute(
            "UPDATE delegated_instances SET balance_mu = NULL, peer_balance_mu = NULL "
            "WHERE token_delegation = ?",
            (self.PEER_TOKEN,),
        )

        self.assertEqual(self.sc.get_delegated_balance(token=self.PEER_TOKEN), 0)
        self.assertEqual(self.sc.get_delegated_balance(token="nope"), 0)
        self.assertEqual(self.sc.get_delegated_instances()[0]['peer_balance_mu'], 0)

    def test_purging_removes_the_row(self):
        self.sc.purgue_delegated(token=self.PEER_TOKEN)

        self.assertIsNone(self.sc.get_delegated_instance(token=self.PEER_TOKEN))
        self.assertEqual(self.sc.get_delegated_instances(), [])

    def test_unknown_tokens_report_absence_instead_of_failing(self):
        self.assertIsNone(self.sc.get_delegated_instance(token="nope"))
        self.assertEqual(self.sc.get_external_father_id(token="nope"), "")
        self.assertIsNone(self.sc.get_delegated_token_by_id(id="nope"))
        self.assertIsNone(self.sc.get_peer_id_by_external_service(token="nope"))


if __name__ == "__main__":
    unittest.main()
