"""A hibernation by the parent keeps the resources of the instance (#669)."""

import sqlite3
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.manager import hibernation
    from src.utils import dead_instances
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HibernateKeepsResourcesTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.sc.add_local_instance(
            father_id="father", container_ip="192.168.200.1", container_id="a", name="a",
            balance_mu=100, serialized_instance="x", service_id="svc", virtualizer="ch",
            mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50, envs="{}",
        )

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def _saved(self):
        row = self.sc._execute(
            "SELECT saved_mem_limit, saved_cpu_period, saved_cpu_quota FROM local_instances "
            "WHERE id = 'a'").fetchone()
        return tuple(row)

    def test_a_hibernation_by_the_parent_saves_the_resources(self):
        self.assertTrue(self.sc.hibernate_local_instance("a"))
        self.assertEqual(self._saved(), (1000, 100, 50))
        row = self.sc.get_sys_req(id="a")
        self.assertEqual((row["mem_limit"], row["cpu_period"], row["cpu_quota"]), (0, 0, 0))

    def test_the_window_does_not_wake_what_the_parent_hibernated(self):
        self.sc.hibernate_local_instance("a")
        self.assertEqual(self.sc.get_window_hibernated_instances(), [])

    def test_the_saved_resources_are_read_for_a_hibernated_instance(self):
        self.assertIsNone(self.sc.get_saved_resources("a"))
        self.sc.hibernate_local_instance("a")
        self.assertEqual(self.sc.get_saved_resources("a"),
                         {"mem_limit": 1000, "cpu_period": 100, "cpu_quota": 50})

    def test_an_instance_hibernated_for_no_balance_has_no_saved_resources_to_wake(self):
        self.sc.hibernate_local_instance("a")
        self.sc.mark_hibernated_unpaid("a")
        self.assertIsNone(self.sc.get_saved_resources("a"))

    def test_waking_gives_the_saved_resources_back(self):
        self.sc.hibernate_local_instance("a")
        with mock.patch.object(hibernation, "wake_instance", return_value=True) as wake:
            self.assertTrue(hibernation.wake_saved_instance("a"))
        token, sysreq = wake.call_args.args
        self.assertEqual(token, "a")
        self.assertEqual((sysreq.mem_limit, sysreq.cpu_period, sysreq.cpu_quota), (1000, 100, 50))

    def test_a_row_with_no_saved_resources_stays_hibernated(self):
        self.sc.hibernate_local_instance("a")
        self.sc._execute("UPDATE local_instances SET saved_mem_limit = NULL, "
                         "saved_cpu_quota = NULL WHERE id = 'a'")
        with mock.patch.object(hibernation, "wake_instance") as wake:
            self.assertFalse(hibernation.wake_saved_instance("a"))
        wake.assert_not_called()
        self.assertTrue(self.sc.is_local_instance_hibernated(id="a"))

    def test_a_running_instance_is_not_woken(self):
        with mock.patch.object(hibernation, "wake_instance") as wake:
            self.assertFalse(hibernation.wake_saved_instance("a"))
        wake.assert_not_called()

    def test_a_wake_clears_the_saved_resources(self):
        self.sc.hibernate_local_instance("a")
        with mock.patch.object(hibernation, "modify_sysreq", return_value=True), \
                mock.patch.object(hibernation, "_revive_locked",
                                  side_effect=lambda token: self.sc.resume_local_instance(token)):
            self.assertTrue(hibernation.wake_saved_instance("a"))
        self.assertEqual(self._saved(), (None, None, None))
        self.assertFalse(self.sc.is_local_instance_hibernated(id="a"))

    def test_a_resize_that_leaves_one_at_zero_changes_nothing(self):
        self.sc.hibernate_local_instance("a")
        sysreq = celaut_pb2.Sysresources(mem_limit=2000, cpu_quota=0)
        self.assertEqual(hibernation.plan("a", sysreq), hibernation.STAY)
        self.assertEqual(self._saved(), (1000, 100, 50))

    def test_a_resize_with_both_above_zero_is_a_wake(self):
        self.sc.hibernate_local_instance("a")
        sysreq = celaut_pb2.Sysresources(mem_limit=2000, cpu_quota=10)
        self.assertEqual(hibernation.plan("a", sysreq), hibernation.WAKE)


if __name__ == "__main__":
    unittest.main()
