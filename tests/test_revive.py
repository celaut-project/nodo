"""An instance whose VM goes down is started again on its own disk (issue #594).

While it has MU it stays afloat: same id, so the same address, MAC and tap; same disk,
so what it wrote is still there; and a service that keeps crashing is retried with a
growing wait that stops growing at a cap, never with a limit on the attempts.
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
    from protos import celaut_pb2 as celaut
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.manager import maintain, manager
    from src.utils import dead_instances
    from src.virtualizers import interface
    from src.virtualizers.ch import execute as ch_execute
    from src.virtualizers.microvm import launch_spec
    from src.virtualizers.microvm.errors import MicroVMError
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


def _resolution():
    entry = celaut.ConfigurationFile.NetworkResolution(tags=["*"])
    return [entry]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchSpecTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_what_the_host_decided_survives_a_round_trip(self):
        launch_spec.write(self.dir, launch_spec.LaunchSpec(
            assigment_ports={8080: 49001, 53: 49002}, by_local=False,
            network_resolution=_resolution(), revivable=True))
        spec = launch_spec.read(self.dir)
        self.assertEqual(spec.assigment_ports, {8080: 49001, 53: 49002})
        self.assertFalse(spec.by_local)
        self.assertTrue(spec.revivable)
        self.assertEqual([list(entry.tags) for entry in spec.network_resolution], [["*"]])

    def test_the_spec_is_readable_by_its_owner_only(self):
        launch_spec.write(self.dir, launch_spec.LaunchSpec())
        self.assertEqual(launch_spec.spec_path(self.dir).stat().st_mode & 0o777, 0o600)

    def test_no_spec_or_a_broken_one_means_not_revivable(self):
        self.assertIsNone(launch_spec.read(self.dir))
        launch_spec.spec_path(self.dir).write_text("{not json")
        self.assertIsNone(launch_spec.read(self.dir))

    def test_an_unwritable_spec_does_not_fail_the_launch(self):
        launch_spec.write(self.dir / "missing" / "dir", launch_spec.LaunchSpec())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RevivableTests(unittest.TestCase):
    def _dir(self, spec=True, rootfs=True, revivable=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime_dir = Path(tmp.name)
        if spec:
            launch_spec.write(runtime_dir, launch_spec.LaunchSpec(revivable=revivable))
        if rootfs:
            (runtime_dir / "rootfs.ext4").write_bytes(b"x")
        return runtime_dir

    def _revivable(self, runtime_dir):
        with mock.patch("src.virtualizers.microvm.paths.runtime_vm_dir", return_value=runtime_dir):
            return interface.revivable("vm")

    def test_a_disk_with_its_spec_is_revivable(self):
        self.assertTrue(self._revivable(self._dir()))

    def test_without_a_disk_a_spec_is_not_enough(self):
        self.assertFalse(self._revivable(self._dir(rootfs=False)))

    def test_without_a_spec_the_disk_is_not_enough(self):
        self.assertFalse(self._revivable(self._dir(spec=False)))

    def test_an_instance_the_launch_marked_not_revivable_is_not(self):
        self.assertFalse(self._revivable(self._dir(revivable=False)))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BackoffTests(unittest.TestCase):
    def setUp(self):
        for table in (maintain._revive_failures, maintain._revive_not_before, maintain._revived_at):
            table.clear()
        self.addCleanup(lambda: [t.clear() for t in (
            maintain._revive_failures, maintain._revive_not_before, maintain._revived_at)])

    def test_the_wait_doubles_and_stops_growing_at_the_cap(self):
        waits = [maintain.revive_backoff_seconds(n) for n in range(0, 12)]
        self.assertEqual(waits[:7], [0, 1, 2, 4, 8, 16, 32])
        self.assertEqual(set(waits[7:]), {60})
        self.assertEqual(maintain.REVIVE_BACKOFF_CAP_S, 60)

    def test_the_first_crash_is_retried_at_once(self):
        maintain._note_instance_down("a", now=1000.0)
        self.assertEqual(maintain._revive_failures["a"], 0)
        self.assertEqual(maintain._revive_not_before["a"], 1000.0)

    def test_a_crash_right_after_a_revive_is_a_loop_and_waits_longer_each_time(self):
        now = 1000.0
        maintain._revived_at["a"] = now
        waits = []
        for _ in range(4):
            maintain._note_instance_down("a", now=now + 5)
            waits.append(maintain._revive_not_before["a"] - (now + 5))
            maintain._revived_at["a"] = now + 5
            now += 5
        self.assertEqual(waits, [1.0, 2.0, 4.0, 8.0])

    def test_there_is_no_limit_on_the_attempts(self):
        now = 1000.0
        for _ in range(500):
            maintain._revived_at["a"] = now
            maintain._note_instance_down("a", now=now + 1)
            now += 1
        self.assertEqual(maintain.revive_backoff_seconds(maintain._revive_failures["a"]), 60.0)

    def test_one_that_ran_a_while_starts_again_from_zero(self):
        maintain._revive_failures["a"] = 9
        maintain._revived_at["a"] = 1000.0
        maintain._note_instance_down("a", now=1000.0 + maintain.REVIVE_STABLE_AFTER_S + 1)
        self.assertEqual(maintain._revive_failures["a"], 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReviveTickTests(unittest.TestCase):
    def setUp(self):
        for table in (maintain._revive_failures, maintain._revive_not_before, maintain._revived_at):
            table.clear()
        self.addCleanup(lambda: [t.clear() for t in (
            maintain._revive_failures, maintain._revive_not_before, maintain._revived_at)])

    def _tick(self, rows, revive, now=1000.0, can_pay=True):
        ids = [row["id"] for row in rows]
        with mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain.sc, "get_all_internal_containers_ids", return_value=ids), \
                mock.patch.object(maintain, "_can_pay_a_tick", return_value=can_pay), \
                mock.patch.object(maintain, "system_scarcity", return_value={}), \
                mock.patch.object(maintain, "revive_instance", side_effect=revive) as revive_mock:
            revived = maintain.revive_suspended_instances(now=now)
        return revived, [call.args[0] for call in revive_mock.call_args_list]

    def test_it_starts_the_ones_that_have_mu_and_leaves_the_ones_that_do_not(self):
        rows = [{"id": "with", "balance_mu": "5"}, {"id": "without", "balance_mu": "0"}]
        revived, tried = self._tick(rows, revive=lambda token: True)
        self.assertEqual((revived, tried), (1, ["with"]))

    def test_one_that_cannot_pay_a_tick_is_not_started_to_be_suspended_again(self):
        # Out of balance is below one tick's charge, not only zero: with 5 MU against a
        # charge of 100 the next tick would suspend it again, a boot and a kill every pass.
        rows = [{"id": "a", "balance_mu": "5"}]
        revived, tried = self._tick(rows, revive=lambda token: True, can_pay=False)
        self.assertEqual((revived, tried), (0, []))

    def test_only_a_few_are_started_per_pass_so_the_tick_is_not_stalled(self):
        rows = [{"id": f"vm-{n}", "balance_mu": "5"} for n in range(5)]
        revived, tried = self._tick(rows, revive=lambda token: True)
        self.assertEqual(revived, maintain.REVIVES_PER_PASS)
        self.assertEqual(len(tried), maintain.REVIVES_PER_PASS)

    def test_one_that_fails_does_not_use_up_the_pass_of_the_next(self):
        rows = [{"id": "bad", "balance_mu": "5"}, {"id": "good", "balance_mu": "5"}]
        revived, tried = self._tick(
            rows, revive=lambda token: (_ for _ in ()).throw(RuntimeError("no boot")) if token == "bad" else True)
        self.assertEqual((revived, tried), (1, ["bad", "good"]))

    def test_one_that_fails_is_not_given_up_and_waits_longer(self):
        rows = [{"id": "a", "balance_mu": "5"}]
        boom = mock.Mock(side_effect=RuntimeError("boot failed"))
        waits = []
        now = 1000.0
        for _ in range(8):
            _revived, tried = self._tick(rows, revive=boom, now=now)
            self.assertEqual(tried, ["a"])
            waits.append(maintain._revive_not_before["a"] - now)
            now = maintain._revive_not_before["a"]
        self.assertEqual(waits, [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])

    def test_it_is_not_tried_again_before_its_wait_is_over(self):
        rows = [{"id": "a", "balance_mu": "5"}]
        boom = mock.Mock(side_effect=RuntimeError("boot failed"))
        self._tick(rows, revive=boom, now=1000.0)
        _revived, tried = self._tick(rows, revive=boom, now=1000.5)
        self.assertEqual(tried, [])

    def test_the_entries_of_an_instance_that_is_gone_are_dropped(self):
        maintain._revive_failures["gone"] = 3
        self._tick([], revive=lambda token: True)
        self.assertNotIn("gone", maintain._revive_failures)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HandleFailureTests(unittest.TestCase):
    def _handle(self, balance, revivable, reason=dead_instances.PROCESS_LOST):
        with mock.patch.object(manager.sc, "get_instance_balance", return_value=balance), \
                mock.patch.object(manager, "revivable", return_value=revivable), \
                mock.patch.object(manager, "suspend_instance") as suspend, \
                mock.patch.object(manager, "stop_instance", return_value=0) as stop:
            outcome = manager.handle_internal_failure("vm", reason)
        return outcome, suspend, stop

    def test_with_gas_and_a_disk_it_is_kept_afloat(self):
        outcome, suspend, stop = self._handle(balance=10, revivable=True)
        self.assertTrue(outcome)
        suspend.assert_called_once_with(token="vm", reason=dead_instances.PROCESS_LOST)
        stop.assert_not_called()

    def test_a_guest_panic_is_an_internal_failure_too(self):
        _outcome, suspend, _stop = self._handle(10, True, reason=dead_instances.GUEST_PANIC)
        suspend.assert_called_once_with(token="vm", reason=dead_instances.GUEST_PANIC)

    def test_without_gas_it_is_stopped(self):
        _outcome, suspend, stop = self._handle(balance=0, revivable=True)
        suspend.assert_not_called()
        stop.assert_called_once_with(token="vm", reason=dead_instances.PROCESS_LOST)

    def test_one_that_cannot_be_revived_is_stopped_as_before(self):
        _outcome, suspend, stop = self._handle(balance=10, revivable=False)
        suspend.assert_not_called()
        stop.assert_called_once()

    def test_one_that_is_already_gone_is_stopped_so_nothing_it_left_is_kept(self):
        with mock.patch.object(manager.sc, "get_instance_balance", return_value=10), \
                mock.patch.object(manager, "revivable", return_value=True), \
                mock.patch.object(manager, "suspend_instance", return_value=False), \
                mock.patch.object(manager.sc, "is_local_instance_suspended", return_value=False), \
                mock.patch.object(manager, "stop_instance", return_value=None) as stop:
            self.assertIsNone(manager.handle_internal_failure("vm", dead_instances.PROCESS_LOST))
        stop.assert_called_once()

    def test_one_that_was_already_suspended_counts_as_handled(self):
        with mock.patch.object(manager.sc, "get_instance_balance", return_value=10), \
                mock.patch.object(manager, "revivable", return_value=True), \
                mock.patch.object(manager, "suspend_instance", return_value=False), \
                mock.patch.object(manager.sc, "is_local_instance_suspended", return_value=True), \
                mock.patch.object(manager, "stop_instance") as stop:
            self.assertTrue(manager.handle_internal_failure("vm", dead_instances.PROCESS_LOST))
        stop.assert_not_called()

    def test_an_unreadable_balance_is_not_gas(self):
        with mock.patch.object(manager.sc, "get_instance_balance", side_effect=Exception("gone")), \
                mock.patch.object(manager, "revivable", return_value=True), \
                mock.patch.object(manager, "suspend_instance") as suspend, \
                mock.patch.object(manager, "stop_instance", return_value=None) as stop:
            self.assertIsNone(manager.handle_internal_failure("vm", dead_instances.PROCESS_LOST))
        suspend.assert_not_called()
        stop.assert_called_once()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReviveInstanceTests(unittest.TestCase):
    def _revive(self, execute_effect=None, suspended=True, room=True):
        calls = []
        service = celaut.Service()
        service.container.resources.at_most.mem_limit = 999
        sys_req = {"mem_limit": 111, "disk_space": 222, "cpu_period": 100, "cpu_quota": 50, "arch": None}

        def record(name, result=None):
            def run(*args, **kwargs):
                calls.append((name, kwargs))
                if isinstance(result, Exception):
                    raise result
                return result
            return run

        with mock.patch.object(manager.sc, "is_local_instance_suspended", return_value=suspended), \
                mock.patch.object(manager.sc, "get_service_id_by_container_id", return_value="svc"), \
                mock.patch.object(manager.sc, "get_internal_father_id", return_value="father"), \
                mock.patch.object(manager.sc, "get_sys_req", return_value=sys_req), \
                mock.patch.object(manager.sc, "get_internal_ip", return_value="192.168.200.7"), \
                mock.patch.object(manager.sc, "resume_local_instance",
                                  side_effect=record("resume", True)), \
                mock.patch("src.utils.utils.read_service_from_disk", return_value=service), \
                mock.patch("src.utils.cost_functions.resource_availability.get_resource_availability",
                           return_value={"can_execute": room, "reason": "memory is taken"}), \
                mock.patch.object(manager, "kill", side_effect=record("kill", True)), \
                mock.patch.object(manager, "vm_execute",
                                  side_effect=execute_effect or record("execute", ("vm", "192.168.200.7", None))):
            try:
                result = manager.revive_instance("vm")
            except Exception as e:
                result = e
        return result, calls

    def test_the_old_vm_goes_first_then_the_same_instance_starts_then_it_is_marked_running(self):
        result, calls = self._revive()
        self.assertIs(result, True)
        self.assertEqual([name for name, _ in calls], ["kill", "execute", "resume"])
        self.assertEqual(calls[0][1], {"vmachine_id": "vm", "archive": False, "keep_disk": True})

    def test_it_starts_the_same_id_with_what_the_row_holds_now(self):
        seen = {}

        def execute(**kwargs):
            seen.update(kwargs)
            return ("vm", "192.168.200.7", None)

        result, _calls = self._revive(execute_effect=execute)
        self.assertIs(result, True)
        self.assertEqual(seen["revive_id"], "vm")
        self.assertEqual(seen["father_id"], "father")
        at_init = seen["system_resources"].at_init
        self.assertEqual((at_init.mem_limit, at_init.disk_space, at_init.cpu_period, at_init.cpu_quota),
                         (111, 222, 100, 50))
        self.assertEqual(seen["system_resources"].at_most.mem_limit, 999)

    def test_a_failed_start_raises_and_leaves_it_suspended(self):
        result, calls = self._revive(execute_effect=mock.Mock(side_effect=MicroVMError("no boot")))
        self.assertIsInstance(result, MicroVMError)
        self.assertNotIn("resume", [name for name, _ in calls])

    def test_it_is_admitted_like_a_launch_and_stays_suspended_when_there_is_no_room(self):
        # A suspended instance holds no memory or CPU, so its room may have been taken.
        result, calls = self._revive(room=False)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("memory is taken", str(result))
        self.assertNotIn("execute", [name for name, _ in calls])
        self.assertNotIn("resume", [name for name, _ in calls])

    def test_an_instance_that_is_not_suspended_is_left_alone(self):
        result, calls = self._revive(suspended=False)
        self.assertIs(result, False)
        self.assertEqual(calls, [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResumeRowTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original_connection = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.sc.add_local_instance(
            father_id="father", container_ip="192.168.200.7", container_id="a", name="web",
            balance_mu=10, serialized_instance="x", service_id="svc", virtualizer="ch",
            disk_space=0, envs="{}")

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original_connection

    def test_the_reason_is_kept_and_a_resume_clears_it(self):
        self.assertTrue(self.sc.suspend_local_instance("a", dead_instances.PROCESS_LOST))
        self.assertEqual(self.sc.get_suspended_local_instances()[0]["suspended_reason"],
                         dead_instances.PROCESS_LOST)
        self.assertTrue(self.sc.resume_local_instance("a"))
        self.assertFalse(self.sc.is_local_instance_suspended("a"))
        self.assertEqual(self.sc.get_running_internal_containers_ids(), ["a"])

    def test_each_revive_is_counted_with_the_reason_it_had(self):
        # Kept in the row, so a restart of the daemon does not reset it (F6 of the
        # review of #595).
        for reason in (dead_instances.PROCESS_LOST, dead_instances.GUEST_PANIC):
            self.sc.suspend_local_instance("a", reason)
            self.sc.resume_local_instance("a")
        row = SQLConnection._connection.execute(
            "SELECT revive_count, last_revive_reason, last_revived_at FROM local_instances "
            "WHERE id = 'a'").fetchone()
        self.assertEqual((row[0], row[1]), (2, dead_instances.GUEST_PANIC))
        self.assertIsNotNone(row[2])

    def test_resuming_a_running_or_dead_instance_does_nothing(self):
        self.assertFalse(self.sc.resume_local_instance("a"))
        self.sc.suspend_local_instance("a")
        self.sc.mark_local_killed("a", "manual_kill")
        self.assertFalse(self.sc.resume_local_instance("a"))

    def test_the_instance_keeps_its_address_through_a_suspension(self):
        self.sc.suspend_local_instance("a", dead_instances.PROCESS_LOST)
        self.sc.resume_local_instance("a")
        self.assertEqual(self.sc.get_internal_ip("a"), "192.168.200.7")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChReviveKeepsTheDiskTests(unittest.TestCase):
    def test_a_revive_that_cannot_start_never_removes_the_disk(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime_dir = Path(tmp.name) / "vm"
        runtime_dir.mkdir()
        (runtime_dir / "rootfs.ext4").write_bytes(b"what the guest wrote")
        # No launch spec: the revive cannot know how the instance was launched.
        bundle = {"arch": "x86_64", "rootfs_path": "r", "kernel_path": "k",
                  "initramfs_path": "i", "rootfs_format": "ext4"}
        patches = [
            mock.patch.object(ch_execute.paths, "runtime_vm_dir", return_value=runtime_dir),
            mock.patch.object(ch_execute.network, "preflight"),
            mock.patch.object(ch_execute, "_resolve_ch_binary", return_value="ch"),
            mock.patch.object(ch_execute.microvm_bundle, "resolve_service_arch", return_value="x86_64"),
            mock.patch.object(ch_execute.microvm_bundle, "load_bundle", return_value=bundle),
            mock.patch.object(ch_execute.microvm_bundle, "validate_custom_initramfs"),
            mock.patch.object(ch_execute.microvm_bundle, "validate_entrypoint_strict", return_value="/run"),
            mock.patch.object(ch_execute.network, "deterministic_ip_and_mac",
                              return_value=("192.168.200.7", "02:00:00:00:00:01")),
            mock.patch.object(ch_execute, "ensure_private_dir"),
            mock.patch.object(ch_execute.network, "replay_legacy_cleanup_rules"),
            mock.patch.object(ch_execute, "vm_remove_vm_rules"),
            mock.patch.object(ch_execute, "release_shares"),
            mock.patch.object(ch_execute, "delete_runtime_state"),
            mock.patch.object(ch_execute, "log"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(MicroVMError):
            ch_execute.execute(
                assigment_ports=None, by_local=True, service_id="svc",
                service=celaut.Service(), config=None,
                system_resources=celaut.Service.Container.Resources(),
                father_id="father", revive_id="vm")
        self.assertEqual((runtime_dir / "rootfs.ext4").read_bytes(), b"what the guest wrote")


if __name__ == "__main__":
    unittest.main()
