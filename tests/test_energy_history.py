"""The ``energy_consumption`` log survives past the current sample (issue #258).

``src/manager/energy/monitor.py`` writes one row per tick and never reads them back
itself -- the TUI's ENERGY page and OVERVIEW card read the table directly. What is
tested here is the one piece of that table's lifecycle that lives in Python:
pruning, so a node running for a year does not carry a year of per-minute rows.
"""
import unittest
from datetime import datetime, timedelta

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EnergyConsumptionPruneTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        # `energy_consumption` is not in `TRACEABILITY_TABLES` -- this project's
        # upgrade path is reinstall, not in-place, so `SQLConnection()` alone does
        # not guarantee the table. A real install always gets it from a full
        # `migrate()`; this test asks for the same table the same way, so it does
        # not depend on whatever this machine's own database already happens to hold.
        from src.database.migrate import ensure_tables

        ensure_tables(self.sc._connection.cursor(), ["energy_consumption"])
        self._clear()
        self.addCleanup(self._clear)

    def _clear(self):
        self.sc._execute("DELETE FROM energy_consumption")

    def _insert_at(self, moment: datetime, watts: float = 10.0) -> None:
        self.sc._execute(
            """
            INSERT INTO energy_consumption
            (timestamp, energy_joules, watts, price_per_kwh, currency, backend, is_floor)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                moment.strftime("%Y-%m-%d %H:%M:%S"),
                watts * 60,
                watts,
                0.15,
                "EUR",
                "rapl",
                0,
            ),
        )

    def _watts(self):
        rows = self.sc._execute(
            "SELECT watts FROM energy_consumption ORDER BY id ASC"
        ).fetchall()
        return [row[0] for row in rows]

    def test_rows_past_keep_days_are_dropped(self):
        now = datetime.utcnow()
        self._insert_at(now - timedelta(days=40), watts=1.0)
        self._insert_at(now - timedelta(days=10), watts=2.0)
        self._insert_at(now, watts=3.0)

        self.sc.prune_energy_consumption(keep_days=30)

        self.assertEqual(self._watts(), [2.0, 3.0], "only the row past the window is dropped")

    def test_nothing_is_dropped_when_everything_is_recent(self):
        now = datetime.utcnow()
        self._insert_at(now - timedelta(hours=1), watts=1.0)
        self._insert_at(now, watts=2.0)

        self.sc.prune_energy_consumption(keep_days=30)

        self.assertEqual(self._watts(), [1.0, 2.0])

    def test_insert_energy_sample_is_what_the_tick_writes(self):
        # The public path the monitor actually calls, exercised once so a change to
        # its column order or defaults fails here rather than only at runtime.
        self.sc.insert_energy_sample(
            energy_joules=600.0,
            watts=10.0,
            price_per_kwh=0.2,
            currency="EUR",
            backend="rapl",
            is_floor=False,
        )
        self.assertEqual(self._watts(), [10.0])


if __name__ == "__main__":
    unittest.main()
