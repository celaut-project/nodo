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
    HASHED = "our-hashed-alias"

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
            encrypted_external_token=self.HASHED,
            external_token=self.PEER_TOKEN,
            peer_id="peer-1",
            serialized_instance=b"original-instance",
            service_id="service-1",
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
        self.assertEqual(
            rows[0],
            {
                'token': self.PEER_TOKEN,
                'id': self.HASHED,
                'peer_id': 'peer-1',
                'father_id': 'father-instance',
                'serialized_instance': b'original-instance',
            },
        )

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
