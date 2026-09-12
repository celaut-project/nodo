"""Storing what the ledgers say: against a real SQLite file, on the real schema.

Two things the balancer's on-chain term depends on and that a mocked cursor cannot show:

* **Replace, not upsert.** An opinion can be *withdrawn* -- revising a reputation box
  spends it and writes a new one -- so a proof that pulled its stake back leaves no row
  to update, and an upsert would keep crediting a peer for a vouch that no longer exists
  on the chain.
* **One transaction.** The balancer takes a single pass over the whole table; a reader
  that landed between the delete and the inserts would see a peer with half its standing
  and rank it as though the network had withdrawn its opinion.

Plus the reading contract the routing path relies on: a failure is an empty list, never
an exception (issues #352, #353).
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.database.migrate import TABLES, INDEXES
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnChainOpinionStorageTests(unittest.TestCase):
    """A real database file, created from the shipped schema."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(prefix="nodo-onchain-", suffix=".db")
        os.close(handle)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        cursor = self.connection.cursor()
        cursor.execute(TABLES["onchain_opinions"])
        for index in INDEXES:
            if "onchain_opinions" in index:
                cursor.execute(index)
        self.connection.commit()

        self.sql = SQLConnection()
        self._patch = mock.patch.object(
            type(self.sql), "_connection", self.connection
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.connection.close()
        os.unlink(self.path)

    def _rows(self, subject="peer-a"):
        return sorted(
            (row["proof_id"], row["verdict"], row["burned_nanoerg"])
            for row in self.sql.get_onchain_opinions(subject)
        )

    def test_the_shipped_schema_carries_the_table(self):
        # Without it the term reads zero for every candidate, which is the safe
        # direction but silent: the node routes as though nobody vouched for anybody.
        self.assertIn("onchain_opinions", TABLES)
        from src.database.sql_connection import TRACEABILITY_TABLES

        self.assertIn(
            "onchain_opinions", TRACEABILITY_TABLES,
            "a node that upgraded in place must get the table without reinstalling",
        )

    def test_rows_are_written_and_read_back(self):
        self.assertTrue(self.sql.replace_onchain_opinions(
            "ergo", "peer-a", [("proof-1", 0.25, 10 ** 15), ("proof-2", -0.1, 10 ** 12)]
        ))
        self.assertEqual(
            self._rows(),
            [("proof-1", 0.25, 10 ** 15), ("proof-2", -0.1, 10 ** 12)],
        )

    def test_a_withdrawn_opinion_disappears_rather_than_lingering(self):
        """The reason this is a replace and not an upsert.

        Revising a reputation box spends it. A proof that pulled its stake back leaves
        nothing behind to update, so an upsert would keep crediting a peer for a vouch
        the chain no longer holds.
        """
        self.sql.replace_onchain_opinions(
            "ergo", "peer-a", [("proof-1", 0.25, 10 ** 15), ("proof-2", 0.25, 10 ** 12)]
        )
        self.sql.replace_onchain_opinions("ergo", "peer-a", [("proof-1", 0.25, 10 ** 15)])
        self.assertEqual(self._rows(), [("proof-1", 0.25, 10 ** 15)])

    def test_an_empty_refresh_clears_a_subject(self):
        self.sql.replace_onchain_opinions("ergo", "peer-a", [("proof-1", 0.25, 10 ** 15)])
        self.sql.replace_onchain_opinions("ergo", "peer-a", [])
        self.assertEqual(self._rows(), [])

    def test_one_subjects_refresh_does_not_touch_another(self):
        self.sql.replace_onchain_opinions("ergo", "peer-a", [("proof-1", 0.25, 10 ** 15)])
        self.sql.replace_onchain_opinions("ergo", "peer-b", [("proof-2", 0.5, 10 ** 12)])
        self.sql.replace_onchain_opinions("ergo", "peer-a", [])
        self.assertEqual(self._rows("peer-a"), [])
        self.assertEqual(self._rows("peer-b"), [("proof-2", 0.5, 10 ** 12)])

    def test_one_proof_speaks_once_per_subject(self):
        """The primary key, not a convention. Writing the same proof twice is refused,
        and the caller nets a publisher's boxes before it gets here."""
        ok = self.sql.replace_onchain_opinions(
            "ergo", "peer-a", [("proof-1", 0.25, 10 ** 15), ("proof-1", 0.25, 10 ** 15)]
        )
        self.assertFalse(ok, "a duplicated proof must not be stored twice")

    def test_a_failed_write_leaves_the_previous_rows_in_place(self):
        """A database error is not the network changing its mind."""
        self.sql.replace_onchain_opinions("ergo", "peer-a", [("proof-1", 0.25, 10 ** 15)])
        self.assertFalse(self.sql.replace_onchain_opinions(
            "ergo", "peer-a", [("proof-2", 0.5, 10 ** 12), ("proof-2", 0.5, 10 ** 12)]
        ))
        self.assertEqual(self._rows(), [("proof-1", 0.25, 10 ** 15)])

    def test_every_subject_is_read_in_one_pass(self):
        self.sql.replace_onchain_opinions("ergo", "peer-a", [("proof-1", 0.25, 10 ** 15)])
        self.sql.replace_onchain_opinions("ergo", "peer-b", [("proof-2", 0.5, 10 ** 12)])
        self.assertEqual(
            sorted(row["subject_id"] for row in self.sql.get_onchain_opinions()),
            ["peer-a", "peer-b"],
        )

    def test_the_burn_survives_the_round_trip_without_losing_precision(self):
        """A nanoERG figure is larger than a float carries exactly, so it is an INTEGER.

        Stored as REAL, a proof that burned a few thousand ERG would come back as a
        different number than it went in as -- and the term multiplies by it.
        """
        burned = 4321 * 10 ** 9 + 7
        self.sql.replace_onchain_opinions("ergo", "peer-a", [("proof-1", 1.0, burned)])
        self.assertEqual(self._rows(), [("proof-1", 1.0, burned)])

    def test_an_unreadable_table_is_an_empty_list_not_an_exception(self):
        # The routing path calls this. It has to complete (issue #352).
        with mock.patch.object(
            self.sql, "_execute", side_effect=sqlite3.Error("database is locked")
        ):
            self.assertEqual(self.sql.get_onchain_opinions(), [])


if __name__ == "__main__":
    unittest.main()
