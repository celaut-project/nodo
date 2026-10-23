"""Tests for dead instances: rows that stay with `killed_at`, and their archive.

The schema comes from ``migrate.create_tables``, so the tests fail if the table and
its accessors diverge. A dead instance must never become alive again.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

IMPORT_ERROR = None
try:
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.utils import dead_instances
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeadInstanceTests(unittest.TestCase):
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

    def _add_local(self, instance_id: str, name: str = "web"):
        self.sc.add_local_instance(
            father_id="father", container_ip="10.0.0.2", container_id=instance_id,
            name=name, balance_mu=100, serialized_instance="x", service_id="svc",
            virtualizer="ch", disk_space=0, envs="{}",
        )

    def test_killed_instance_is_not_alive(self):
        self._add_local("a")
        self.assertTrue(self.sc.internal_instance_exists("a"))
        self.assertTrue(self.sc.mark_local_killed("a", "manual_kill"))
        self.assertFalse(self.sc.internal_instance_exists("a"))
        self.assertEqual(self.sc.get_all_internal_containers_ids(), [])
        self.assertEqual(self.sc.get_committed_resources(), [])
        self.assertIsNone(self.sc.resolve_local_instance_reference("web"))

    def test_kill_is_idempotent(self):
        self._add_local("a")
        self.assertTrue(self.sc.mark_local_killed("a", "manual_kill"))
        self.assertFalse(self.sc.mark_local_killed("a", "out_of_balance"))
        self.assertEqual(self.sc.get_dead_local_instances()[0]["death_reason"], "manual_kill")

    def test_dead_row_cannot_be_revived(self):
        self._add_local("a")
        self.sc.mark_local_killed("a", "manual_kill")
        with self.assertRaises(sqlite3.Error):
            self.sc._execute("UPDATE local_instances SET killed_at = NULL WHERE id = 'a'")
        with self.assertRaises(sqlite3.Error):
            self.sc._execute("UPDATE local_instances SET balance_mu = '5' WHERE id = 'a'")

    def test_balance_of_dead_instance_is_not_spent(self):
        self._add_local("a")
        self.sc.mark_local_killed("a", "manual_kill")
        with self.assertRaises(ledger.NoSuchAccount):
            self.sc.move_balance("a", -1, ledger.MAINTENANCE, allow_debt=False)

    def test_name_of_dead_instance_can_be_used_again(self):
        self._add_local("a")
        self.sc.mark_local_killed("a", "manual_kill")
        self._add_local("b")
        with self.assertRaises(sqlite3.IntegrityError):
            self._add_local("c")

    def _date_death(self, instance_id: str, moment: str):
        """Set `killed_at` by hand. The trigger would refuse it, so drop it first."""
        self.sc._execute("DROP TRIGGER IF EXISTS trg_local_instances_no_revive")
        self.sc._execute(
            "UPDATE local_instances SET killed_at = ? WHERE id = ?", (moment, instance_id)
        )

    def test_dead_reference_resolves_to_newest_death(self):
        self._add_local("a")
        self.sc.mark_local_killed("a", "manual_kill")
        self._add_local("b")
        self.sc.mark_local_killed("b", "out_of_balance")
        self._date_death("a", "2026-01-01 00:00:00")
        self._date_death("b", "2026-01-02 00:00:00")
        self.assertEqual(self.sc.get_dead_local_instance_by_reference("web")["id"], "b")
        self.assertEqual(self.sc.get_dead_local_instance_by_reference("a")["id"], "a")

    def test_prune_by_count_keeps_the_newest(self):
        for index in range(4):
            self._add_local(f"id{index}", name=f"n{index}")
            self.sc.mark_local_killed(f"id{index}", "manual_kill")
            self._date_death(f"id{index}", f"2026-01-0{index + 1} 00:00:00")
        deleted = self.sc.prune_dead_local_instances(max_age_days=0, max_rows=2)
        self.assertEqual(sorted(deleted), ["id0", "id1"])
        self.assertEqual(
            sorted(row["id"] for row in self.sc.get_dead_local_instances()), ["id2", "id3"]
        )

    def test_prune_by_age_ignores_live_instances(self):
        self._add_local("live", name="live")
        self._add_local("old", name="old")
        self.sc.mark_local_killed("old", "manual_kill")
        self._date_death("old", "2000-01-01 00:00:00")
        self.assertEqual(self.sc.prune_dead_local_instances(max_age_days=14, max_rows=0), ["old"])
        self.assertTrue(self.sc.internal_instance_exists("live"))

    def test_prune_dry_run_deletes_nothing(self):
        self._add_local("a")
        self.sc.mark_local_killed("a", "manual_kill")
        would = self.sc.prune_dead_local_instances(
            max_age_days=0, max_rows=0, dry_run=True, delete_all=True
        )
        self.assertEqual(would, ["a"])
        self.assertEqual(len(self.sc.get_dead_local_instances()), 1)

    def test_prune_all_deletes_every_dead_row_and_no_live_row(self):
        self._add_local("live", name="live")
        self._add_local("a", name="a")
        self.sc.mark_local_killed("a", "manual_kill")
        deleted = self.sc.prune_dead_local_instances(max_age_days=0, max_rows=0, delete_all=True)
        self.assertEqual(deleted, ["a"])
        self.assertTrue(self.sc.internal_instance_exists("live"))

    def test_delegated_instance_kill_keeps_the_row(self):
        self.sc.add_delegated_instance(
            external_token="token", encrypted_external_token="hashed", peer_id="peer",
            father_id="father", serialized_instance=b"", service_id="svc",
            balance_mu=10, peer_balance_mu=10,
        )
        self.assertEqual(len(self.sc.get_delegated_instances()), 1)
        self.assertTrue(self.sc.mark_delegated_killed("token", "peer_stopped"))
        self.assertFalse(self.sc.mark_delegated_killed("token", "peer_stopped"))
        self.assertEqual(self.sc.get_delegated_instances(), [])
        dead = self.sc.get_dead_delegated_instances()
        self.assertEqual(dead[0]["death_reason"], "peer_stopped")
        self.assertEqual(self.sc.get_delegated_balance("token"), 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeadInstanceArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(
            dead_instances, "dead_root", return_value=Path(self._tmp.name) / "instances_dead"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_archive_keeps_only_the_tail_of_a_log(self):
        source = Path(self._tmp.name) / "out.log"
        source.write_bytes(b"a" * 100 + b"END")
        with mock.patch.object(dead_instances, "log_max_bytes", return_value=10):
            dead_instances.archive_runtime("abc", {"stdout_log": str(source), "pid": 1})
        saved = dead_instances.list_logs("abc")["stdout"].read_bytes()
        self.assertEqual(saved, b"a" * 7 + b"END")

    def test_archive_with_empty_state_changes_nothing(self):
        dead_instances.archive_runtime("abc", {})
        self.assertEqual(dead_instances.list_logs("abc"), {})

    def test_death_record_round_trip(self):
        dead_instances.write_death_record("abc", {"reason": "guest_panic"})
        self.assertEqual(dead_instances.read_death_record("abc")["reason"], "guest_panic")

    def test_unsafe_id_is_refused(self):
        with self.assertRaises(ValueError):
            dead_instances.dead_dir("../escape")

    def test_orphan_directories_are_swept(self):
        dead_instances.write_death_record("keep", {})
        dead_instances.write_death_record("orphan", {})
        self.assertEqual(dead_instances.sweep_orphan_dirs({"keep"}), ["orphan"])
        self.assertTrue(os.path.isdir(dead_instances.dead_dir("keep")))


if __name__ == "__main__":
    unittest.main()
