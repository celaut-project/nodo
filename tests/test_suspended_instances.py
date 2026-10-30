"""An instance that runs out of MU is suspended, not killed (issue #594).

Its VM stops, but the row stays live -- so its address stays reserved, its name stays
taken and its account stays open to a top-up -- and the disk stays where it is until
the grace period ends, the operator burns it, or somebody kills it.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.manager import maintain, manager
    from src.utils import dead_instances
    from src.virtualizers.microvm import kill as microvm_kill
    from src.virtualizers.microvm import maintain as microvm_maintain
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SuspensionRowTests(unittest.TestCase):
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

    def _add_local(self, instance_id: str, name: str = "web", ip: str = "192.168.200.7",
                   balance: int = 100):
        self.sc.add_local_instance(
            father_id="father", container_ip=ip, container_id=instance_id,
            name=name, balance_mu=balance, serialized_instance="x", service_id="svc",
            virtualizer="ch", mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50,
            envs="{}",
        )

    def test_a_suspended_instance_is_still_a_live_row(self):
        self._add_local("a")
        self.assertTrue(self.sc.suspend_local_instance("a"))
        self.assertTrue(self.sc.internal_instance_exists("a"))
        self.assertTrue(self.sc.is_local_instance_suspended("a"))
        self.assertEqual(self.sc.get_dead_local_instances(), [])

    def test_suspending_twice_changes_nothing(self):
        self._add_local("a")
        self.assertTrue(self.sc.suspend_local_instance("a"))
        self.assertFalse(self.sc.suspend_local_instance("a"))

    def test_a_dead_instance_cannot_be_suspended(self):
        self._add_local("a")
        self.sc.mark_local_killed("a", "manual_kill")
        self.assertFalse(self.sc.suspend_local_instance("a"))

    def test_the_tick_only_sees_the_instances_that_should_be_running(self):
        self._add_local("running", name="one", ip="192.168.200.2")
        self._add_local("suspended", name="two", ip="192.168.200.3")
        self.sc.suspend_local_instance("suspended")
        self.assertEqual(self.sc.get_running_internal_containers_ids(), ["running"])
        # Everything else that asks "who is alive" still sees both.
        self.assertCountEqual(self.sc.get_all_internal_containers_ids(), ["running", "suspended"])

    def test_a_suspended_instance_keeps_its_address_reserved_and_its_name_taken(self):
        self._add_local("a", ip="192.168.200.9")
        self.sc.suspend_local_instance("a")
        self.assertEqual(self.sc.get_internal_ip("a"), "192.168.200.9")
        with self.assertRaises(sqlite3.IntegrityError):
            self._add_local("b", name="web", ip="192.168.200.10")

    def test_a_suspended_instance_holds_disk_but_not_memory_or_cpu(self):
        self._add_local("a")
        before = self.sc.get_committed_resources()[0]
        self.assertEqual((before["mem_limit"], before["cpu_quota"]), (1000, 50))
        self.sc.suspend_local_instance("a")
        after = self.sc.get_committed_resources()[0]
        self.assertEqual((after["mem_limit"], after["cpu_quota"]), (0, 0))
        self.assertEqual(after["disk_space"], 500)

    def test_a_top_up_reaches_a_suspended_instance(self):
        self._add_local("a", balance=0)
        self.sc.suspend_local_instance("a")
        self.sc.move_balance("a", 25, ledger.CREDIT)
        self.assertEqual(self.sc.get_suspended_local_instances()[0]["balance_mu"], "25")

    def test_a_kill_still_ends_a_suspended_instance_for_good(self):
        self._add_local("a")
        self.sc.suspend_local_instance("a")
        self.assertTrue(self.sc.mark_local_killed("a", "manual_kill"))
        self.assertEqual(self.sc.get_suspended_local_instances(), [])
        self.assertEqual(self.sc.get_all_internal_containers_ids(), [])

    def test_a_node_upgraded_in_place_adds_the_column_without_nodo_migrate(self):
        from src.database import sql_connection
        self.assertEqual(
            sql_connection.TRACEABILITY_COLUMNS["local_instances"],
            {"suspended_at": "DATETIME DEFAULT NULL", "suspended_reason": "TEXT DEFAULT NULL",
             "revive_count": "INTEGER NOT NULL DEFAULT 0", "last_revive_reason": "TEXT DEFAULT NULL",
             "last_revived_at": "DATETIME DEFAULT NULL"})

    def test_an_old_database_gets_the_column(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE local_instances (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "killed_at DATETIME DEFAULT NULL, death_reason TEXT DEFAULT NULL)")
        from src.database.migrate import ensure_columns
        ensure_columns(connection.cursor(), "local_instances",
                       {"suspended_at": "DATETIME DEFAULT NULL"})
        columns = [row[1] for row in connection.execute("PRAGMA table_info(local_instances)")]
        self.assertIn("suspended_at", columns)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GraceTests(unittest.TestCase):
    def _reap(self, rows, grace, can_pay=True):
        with mock.patch.object(maintain.dead_instances, "suspended_grace_seconds", return_value=grace), \
                mock.patch.object(maintain, "_can_pay_a_tick", return_value=can_pay), \
                mock.patch.object(maintain, "system_scarcity", return_value={}), \
                mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "stop_instance", return_value=0) as stop:
            deleted = maintain.reap_expired_suspensions()
        return deleted, [call.kwargs for call in stop.call_args_list]

    def test_it_waits_until_the_grace_period_is_over(self):
        deleted, stops = self._reap(
            [{"id": "a", "balance_mu": "0", "suspended_for_s": 99}], grace=100)
        self.assertEqual((deleted, stops), (0, []))

    def test_it_deletes_it_once_the_grace_period_is_over(self):
        deleted, stops = self._reap(
            [{"id": "a", "balance_mu": "0", "suspended_for_s": 100}], grace=100)
        self.assertEqual(deleted, 1)
        self.assertEqual(stops, [{"token": "a", "reason": dead_instances.OUT_OF_BALANCE}])

    def test_one_that_was_topped_up_is_never_deleted_however_long_it_waited(self):
        deleted, stops = self._reap(
            [{"id": "a", "balance_mu": "7", "suspended_for_s": 10 ** 9}], grace=100)
        self.assertEqual((deleted, stops), (0, []))

    def test_one_that_holds_a_little_but_cannot_pay_a_tick_is_deleted_too(self):
        # Out of balance is "below one tick's charge", not only zero: left alone, it
        # would be suspended again on every tick and never deleted.
        deleted, stops = self._reap(
            [{"id": "a", "balance_mu": "5", "suspended_for_s": 500}], grace=100, can_pay=False)
        self.assertEqual(deleted, 1)
        self.assertEqual([stop["token"] for stop in stops], ["a"])

    def test_one_lost_with_less_than_a_tick_dies_with_the_cause_it_was_suspended_for(self):
        # Its VM went down with MU left, but not a tick's worth: it was never revived for
        # lack of MU, and the cause on record is still the failure (F11 of the review
        # of #595).
        deleted, stops = self._reap(
            [{"id": "a", "balance_mu": "5", "suspended_for_s": 500,
              "suspended_reason": dead_instances.PROCESS_LOST}], grace=100, can_pay=False)
        self.assertEqual(deleted, 1)
        self.assertEqual(stops, [{"token": "a", "reason": dead_instances.PROCESS_LOST}])

    def test_one_that_cannot_be_deleted_does_not_stop_the_others(self):
        rows = [{"id": "a", "balance_mu": "0", "suspended_for_s": 500},
                {"id": "b", "balance_mu": "0", "suspended_for_s": 500}]
        with mock.patch.object(maintain.dead_instances, "suspended_grace_seconds", return_value=1), \
                mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "stop_instance",
                                  side_effect=[RuntimeError("busy"), 0]) as stop:
            self.assertEqual(maintain.reap_expired_suspensions(), 1)
        self.assertEqual(stop.call_count, 2)

    def test_the_grace_period_comes_from_the_config_and_zero_means_none(self):
        with mock.patch.object(dead_instances.env_manager, "get", return_value=0):
            self.assertEqual(dead_instances.suspended_grace_seconds(), 0)
        with mock.patch.object(dead_instances.env_manager, "get", return_value=-5):
            self.assertEqual(dead_instances.suspended_grace_seconds(), 0)
        with mock.patch.object(dead_instances.env_manager, "get", return_value="junk"):
            self.assertEqual(dead_instances.suspended_grace_seconds(),
                             dead_instances.DEFAULT_SUSPENDED_GRACE_SECONDS)
        self.assertEqual(dead_instances.DEFAULT_SUSPENDED_GRACE_SECONDS, 7 * 24 * 3600)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SuspendInstanceTests(unittest.TestCase):
    def test_the_vm_stops_before_the_row_is_marked(self):
        order = []
        with mock.patch.object(manager.sc, "internal_instance_exists", return_value=True), \
                mock.patch.object(manager, "kill", side_effect=lambda **kw: order.append(("kill", kw))), \
                mock.patch.object(manager.sc, "suspend_local_instance",
                                  side_effect=lambda token, reason: order.append(("mark", token, reason)) or True):
            self.assertTrue(manager.suspend_instance("a"))
        self.assertEqual([step[0] for step in order], ["kill", "mark"])
        self.assertEqual(order[1], ("mark", "a", dead_instances.OUT_OF_BALANCE))
        self.assertEqual(order[0][1], {"vmachine_id": "a", "archive": False, "keep_disk": True})

    def test_an_instance_that_is_gone_is_left_alone(self):
        with mock.patch.object(manager.sc, "internal_instance_exists", return_value=False), \
                mock.patch.object(manager, "kill") as kill:
            self.assertFalse(manager.suspend_instance("a"))
        kill.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class KeepDiskTests(unittest.TestCase):
    def _kill(self, keep_disk):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime_dir = Path(tmp.name) / "vm"
        runtime_dir.mkdir()
        (runtime_dir / "rootfs.ext4").write_bytes(b"data")
        hypervisor = mock.MagicMock()
        hypervisor.log.side_effect = lambda _id, message: message
        hypervisor.control_socket.return_value = Path(tmp.name) / "sock"
        state = {"pid": 4242, "tap": "tap0", "cgroup_path": "/cg", "virtiofs": []}
        patches = [
            mock.patch.object(microvm_kill, "load_runtime_state", return_value=state),
            mock.patch.object(microvm_kill, "_runtime_dir", return_value=runtime_dir),
            mock.patch.object(microvm_kill, "_kill_pid"),
            mock.patch.object(microvm_kill, "_cleanup_vm_firewall_rules"),
            mock.patch.object(microvm_kill, "_cleanup_tap"),
            mock.patch.object(microvm_kill, "remove_vm_cgroup"),
            mock.patch.object(microvm_kill.network, "replay_legacy_cleanup_rules"),
            mock.patch.object(microvm_kill, "delete_runtime_state"),
            mock.patch.object(microvm_kill.dead_instances, "archive_runtime"),
        ]
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        microvm_kill.kill(hypervisor, "vm", archive=True, keep_disk=keep_disk)
        return runtime_dir, started

    def test_keeping_the_disk_stops_the_guest_and_leaves_the_image(self):
        runtime_dir, started = self._kill(keep_disk=True)
        kill_pid, firewall, tap, cgroup, _replay, delete_state, archive = started[2:]
        self.assertTrue((runtime_dir / "rootfs.ext4").exists())
        for released in (kill_pid, firewall, tap, cgroup, delete_state):
            released.assert_called_once()
        archive.assert_not_called()

    def test_without_it_the_disk_goes_as_before(self):
        runtime_dir, started = self._kill(keep_disk=False)
        self.assertFalse(runtime_dir.exists())
        started[-1].assert_called_once()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LogsOfTheRunThatFailedTests(unittest.TestCase):
    """A revive used to truncate the logs of the run that caused it, and the kill that
    ended the instance later archived nothing (F5 of the review of #595)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime_dir = self.root / "vm"
        self.runtime_dir.mkdir()
        self.serial = self.runtime_dir / "cloud-hypervisor.serial.log"
        self.stdout = self.runtime_dir / "cloud-hypervisor.stdout.log"
        self.state = {"pid": 4242, "serial_log": str(self.serial), "stdout_log": str(self.stdout)}
        self.hypervisor = mock.MagicMock()
        self.hypervisor.log.side_effect = lambda _id, message: message
        self.hypervisor.control_socket.return_value = self.root / "sock"
        for patch in (
            mock.patch.object(microvm_kill, "_runtime_dir", return_value=self.runtime_dir),
            mock.patch.object(microvm_kill, "_kill_pid"),
            mock.patch.object(microvm_kill, "_cleanup_vm_firewall_rules"),
            mock.patch.object(microvm_kill, "_cleanup_tap"),
            mock.patch.object(microvm_kill, "remove_vm_cgroup"),
            mock.patch.object(microvm_kill.network, "replay_legacy_cleanup_rules"),
            mock.patch.object(microvm_kill, "delete_runtime_state"),
            mock.patch.object(dead_instances, "dead_root", return_value=self.root / "dead"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _kill(self, state, keep_disk):
        with mock.patch.object(microvm_kill, "load_runtime_state", return_value=state):
            microvm_kill.kill(self.hypervisor, "vm", archive=not keep_disk, keep_disk=keep_disk)

    def test_a_suspension_keeps_the_logs_of_the_run_that_ended(self):
        self.serial.write_text("Kernel panic - not syncing\n")
        self._kill(self.state, keep_disk=True)
        self.assertEqual(Path(str(self.serial) + ".1").read_text(), "Kernel panic - not syncing\n")
        self.assertFalse(self.serial.exists())

    def test_the_death_after_a_suspension_archives_both_runs(self):
        self.serial.write_text("first run panicked\n")
        self._kill(self.state, keep_disk=True)
        self.serial.write_text("second run\n")     # the revive
        self._kill({}, keep_disk=False)              # its state file went with the suspension
        logs = dead_instances.list_logs("vm")
        self.assertEqual(logs["serial"].read_text(), "second run\n")
        self.assertEqual(logs["serial.previous"].read_text(), "first run panicked\n")
        self.assertFalse(self.runtime_dir.exists())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PruneLeavesSuspendedDisksTests(unittest.TestCase):
    def test_a_suspended_instances_runtime_dir_is_not_reclaimable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kept, stray = Path(tmp.name) / "kept", Path(tmp.name) / "stray"
        kept.mkdir()
        stray.mkdir()
        with mock.patch.object(microvm_maintain, "list_runtime_states", return_value={}), \
                mock.patch.object(microvm_maintain, "list_runtime_dirs",
                                  return_value={"kept": kept, "stray": stray}), \
                mock.patch.object(microvm_maintain, "_age_seconds", return_value=10 ** 9), \
                mock.patch.object(microvm_maintain, "_dir_size", return_value=1), \
                mock.patch.object(microvm_maintain.sc, "is_local_instance_suspended",
                                  side_effect=lambda id: id == "kept"):
            ids = [entry.vmachine_id for entry in microvm_maintain.scan_orphan_runtimes()]
        self.assertEqual(ids, ["stray"])

    def test_when_it_cannot_tell_it_keeps_the_disk(self):
        with mock.patch.object(microvm_maintain.sc, "is_local_instance_suspended",
                               side_effect=sqlite3.Error("locked")):
            self.assertTrue(microvm_maintain._is_suspended("anything"))


if __name__ == "__main__":
    unittest.main()
