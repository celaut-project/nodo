"""`ON_CLOSE: hibernate` hibernates at closing and wakes at opening (issue #344, phase 1)."""

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
    from src.gateway import gateway
    from src.manager import hibernation, maintain
    from src.utils import dead_instances
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnCloseHibernateTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        for token in ("a", "b"):
            self.sc.add_local_instance(
                father_id="father", container_ip=f"192.168.200.{ord(token)}", container_id=token,
                name=token, balance_mu=100, serialized_instance="x", service_id="svc",
                virtualizer="ch", mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50,
                envs="{}",
            )

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def _closed(self, hibernate=True, dev=()):
        stack = [
            mock.patch.object(maintain.activity_window, "is_open", return_value=False),
            mock.patch.object(maintain.activity_window, "on_close_policy",
                              return_value="hibernate" if hibernate else "refuse"),
            mock.patch.object(maintain, "sc", self.sc),
            mock.patch.object(maintain, "descends_from_dev_client",
                              side_effect=lambda token: token in dev),
        ]
        return stack

    def _enforce(self, **kwargs):
        patches = self._closed(**kwargs)
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(hibernation, "check_can_hibernate"), \
                    mock.patch.object(hibernation, "kill") as kill, \
                    mock.patch.object(maintain, "stop_instance") as stop:
                maintain.enforce_activity_window()
            return kill, stop
        finally:
            for patch in patches:
                patch.stop()

    def test_closing_hibernates_each_instance_and_saves_its_resources(self):
        self._enforce()
        for token in ("a", "b"):
            row = self.sc.get_sys_req(id=token)
            self.assertEqual((row["mem_limit"], row["cpu_period"], row["cpu_quota"]), (0, 0, 0))
            self.assertEqual(row["disk_space"], 500)
        saved = {row["id"]: row for row in self.sc.get_window_hibernated_instances()}
        self.assertEqual(set(saved), {"a", "b"})
        self.assertEqual((saved["a"]["mem_limit"], saved["a"]["cpu_period"],
                          saved["a"]["cpu_quota"]), (1000, 100, 50))

    def test_a_second_tick_does_not_stop_the_vm_again_or_lose_the_saved_resources(self):
        self._enforce()
        kill, _ = self._enforce()
        kill.assert_not_called()
        self.assertEqual(self.sc.get_window_hibernated_instances()[0]["mem_limit"], 1000)

    def test_a_dev_descendant_is_not_touched(self):
        self._enforce(dev=("a",))
        self.assertFalse(self.sc.is_local_instance_hibernated(id="a"))
        self.assertTrue(self.sc.is_local_instance_hibernated(id="b"))

    def test_an_instance_that_cannot_hibernate_is_stopped_with_the_window_reason(self):
        patches = self._closed()
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(hibernation, "check_can_hibernate",
                                   side_effect=hibernation.HibernationRefused("no disk")), \
                    mock.patch.object(maintain, "stop_instance") as stop:
                maintain.enforce_activity_window()
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(
            sorted(call.kwargs["token"] for call in stop.call_args_list), ["a", "b"])
        for call in stop.call_args_list:
            self.assertEqual(call.kwargs["reason"], dead_instances.ACTIVITY_WINDOW)

    def _open(self):
        with mock.patch.object(maintain.activity_window, "is_open", return_value=True), \
                mock.patch.object(hibernation, "wake_instance", return_value=True) as wake:
            maintain.enforce_activity_window()
        return wake

    def test_opening_wakes_the_instances_with_the_saved_resources(self):
        self._enforce()
        wake = self._open()
        self.assertEqual(wake.call_count, 2)
        token, sysreq = wake.call_args_list[0].args
        self.assertEqual(token, "a")
        self.assertEqual((sysreq.mem_limit, sysreq.cpu_period, sysreq.cpu_quota), (1000, 100, 50))

    def test_opening_does_not_wake_what_the_client_hibernated(self):
        self.sc.hibernate_local_instance("a")
        self._enforce()
        wake = self._open()
        self.assertEqual([call.args[0] for call in wake.call_args_list], ["b"])

    def test_a_failed_wake_leaves_it_hibernated_and_the_next_tick_tries_again(self):
        self._enforce()
        with mock.patch.object(maintain.activity_window, "is_open", return_value=True), \
                mock.patch.object(hibernation, "wake_instance", return_value=False) as wake:
            maintain.enforce_activity_window()
            maintain.enforce_activity_window()
        self.assertEqual(wake.call_count, 4)
        self.assertEqual(len(self.sc.get_window_hibernated_instances()), 2)

    def test_a_real_wake_runs_the_instance_again_and_clears_the_saved_resources(self):
        self._enforce()
        with mock.patch.object(maintain.activity_window, "is_open", return_value=True), \
                mock.patch.object(hibernation, "modify_sysreq", return_value=True), \
                mock.patch.object(hibernation, "_revive_locked",
                                  side_effect=lambda token: self.sc.resume_local_instance(token)):
            maintain.enforce_activity_window()
        self.assertEqual(self.sc.get_window_hibernated_instances(), [])
        self.assertFalse(self.sc.is_local_instance_hibernated(id="a"))

    def test_refuse_and_stop_modes_never_hibernate(self):
        self._enforce(hibernate=False)
        self.assertEqual(self.sc.get_window_hibernated_instances(), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ClosedRefusesGrowthTests(OnCloseHibernateTests):
    def test_growth_is_detected_only_when_a_field_exceeds_the_row(self):
        grow = celaut_pb2.Sysresources(mem_limit=2000)
        same = celaut_pb2.Sysresources(mem_limit=1000, cpu_quota=50)
        less = celaut_pb2.Sysresources(mem_limit=10)
        self.assertTrue(hibernation.is_growth("a", grow))
        self.assertFalse(hibernation.is_growth("a", same))
        self.assertFalse(hibernation.is_growth("a", less))
        self.assertFalse(hibernation.is_growth("a", celaut_pb2.Sysresources()))


if __name__ == "__main__":
    unittest.main()
