"""The one-shot migration that forgets peer rates stored per whole unit (#467).

``ContractRate.mu_per_unit`` went from MU per whole unit to MU per base unit with no
marker on the wire, so a stored peer rate cannot be told apart from a new one. These
pin that every peer's stored rate is cleared exactly once and this node's own never.
"""
import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database.migrate import forget_peer_rates_per_whole_unit
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ForgetPeerRatesTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.cursor = self.db.cursor()
        self.cursor.execute(
            "CREATE TABLE contract_instance (id INTEGER PRIMARY KEY, address TEXT, "
            "ledger TEXT, contract_hash TEXT, token_id TEXT, peer_id TEXT, mu_per_unit TEXT)"
        )
        self.cursor.executemany(
            "INSERT INTO contract_instance (ledger, peer_id, mu_per_unit) VALUES (?,?,?)",
            [("ergo", "LOCAL", "1"), ("ergo", "peer-a", "1000000000"), ("bitcoin", "peer-b", "100000000")],
        )

    def _rates(self):
        self.cursor.execute("SELECT peer_id, mu_per_unit FROM contract_instance ORDER BY id")
        return self.cursor.fetchall()

    def test_peer_rates_are_cleared_and_ours_kept(self):
        forget_peer_rates_per_whole_unit(self.cursor)
        self.assertEqual(self._rates(), [("LOCAL", "1"), ("peer-a", None), ("peer-b", None)])

    def test_it_runs_once(self):
        forget_peer_rates_per_whole_unit(self.cursor)
        # A peer announces again, per base unit; a restart must not forget it.
        self.cursor.execute("UPDATE contract_instance SET mu_per_unit = '1' WHERE peer_id = 'peer-a'")
        forget_peer_rates_per_whole_unit(self.cursor)
        self.assertEqual(self._rates()[1], ("peer-a", "1"))

    def test_a_database_without_the_table_yet_is_not_marked(self):
        self.cursor.execute("DROP TABLE contract_instance")
        forget_peer_rates_per_whole_unit(self.cursor)
        self.cursor.execute("SELECT COUNT(*) FROM applied_migrations")
        self.assertEqual(self.cursor.fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
