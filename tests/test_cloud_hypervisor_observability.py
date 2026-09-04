import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from src.virtualizers.microvm import observability as ch_obs
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ch_obs = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloudHypervisorObservabilityTests(unittest.TestCase):
    def test_snapshot_returns_process_metrics_when_alive(self):
        state = {
            "pid": 321,
            "created_at": "2026-03-23T10:00:00+00:00",
            "stdout_log": "/tmp/vm.stdout.log",
            "stderr_log": "/tmp/vm.stderr.log",
            "serial_log": "/tmp/vm.serial.log",
        }

        proc = MagicMock()
        proc.is_running.return_value = True
        proc.status.return_value = "running"
        proc.create_time.return_value = 1000.0
        proc.memory_info.return_value = MagicMock(rss=123456789)

        with patch.object(ch_obs, "pid_alive", return_value=True), patch.object(
            ch_obs.psutil, "Process", return_value=proc
        ), patch.object(
            ch_obs.time,
            "time",
            return_value=1060.0,
        ):
            snapshot = ch_obs.get_vm_runtime_snapshot(vmachine_id="vm-1", state=state)

        self.assertEqual(snapshot["pid"], 321)
        self.assertTrue(snapshot["alive"])
        self.assertEqual(snapshot["uptime_s"], 60)
        self.assertEqual(snapshot["mem_rss_bytes"], 123456789)
        self.assertEqual(snapshot["log_paths"]["stdout"], "/tmp/vm.stdout.log")
        self.assertEqual(snapshot["log_paths"]["stderr"], "/tmp/vm.stderr.log")
        self.assertEqual(snapshot["log_paths"]["serial"], "/tmp/vm.serial.log")
        self.assertIsNone(snapshot["cgroup_memory_max_raw"])
        self.assertIsNone(snapshot["cgroup_memory_max_bytes"])
        self.assertIsNone(snapshot["cgroup_memory_current_bytes"])

    def test_snapshot_fallback_when_pid_missing(self):
        created_at = ch_obs.datetime.now(ch_obs.timezone.utc).isoformat()
        state = {"pid": 0, "created_at": created_at}
        snapshot = ch_obs.get_vm_runtime_snapshot(vmachine_id="vm-2", state=state)

        self.assertIsNone(snapshot["pid"])
        self.assertFalse(snapshot["alive"])
        self.assertIsInstance(snapshot["uptime_s"], int)
        self.assertGreaterEqual(snapshot["uptime_s"], 0)
        self.assertIsNone(snapshot["mem_rss_bytes"])

    def test_snapshot_handles_psutil_errors_safely(self):
        state = {"pid": 654}
        with patch.object(ch_obs, "pid_alive", return_value=True), patch.object(
            ch_obs.psutil, "Process", side_effect=Exception("boom")
        ):
            snapshot = ch_obs.get_vm_runtime_snapshot(vmachine_id="vm-3", state=state)

        self.assertEqual(snapshot["pid"], 654)
        self.assertFalse(snapshot["alive"])
        self.assertIsNone(snapshot["mem_rss_bytes"])

    def test_snapshot_rejects_reused_pid(self):
        # The identity check runs off the name the launcher recorded, so an entry
        # carries one; a PID alone can belong to whatever inherited it.
        state = {"pid": 654, "process_name": "nodo-ch-abcdef01"}
        with patch.object(ch_obs, "pid_alive", return_value=False), patch.object(
            ch_obs.psutil,
            "Process",
        ) as process_mock:
            snapshot = ch_obs.get_vm_runtime_snapshot(vmachine_id="vm-reused", state=state)

        self.assertEqual(snapshot["pid"], 654)
        self.assertFalse(snapshot["alive"])
        self.assertIsNone(snapshot["mem_rss_bytes"])
        process_mock.assert_not_called()

    def test_snapshot_reads_cgroup_memory_limit_and_current(self):
        with TemporaryDirectory() as tmpdir:
            cgroup_path = Path(tmpdir) / "nodo-ch" / "vm-cgroup"
            cgroup_path.mkdir(parents=True, exist_ok=True)
            (cgroup_path / "memory.max").write_text("50000000\n", encoding="utf-8")
            (cgroup_path / "memory.current").write_text("33000000\n", encoding="utf-8")

            state = {"pid": 0, "cgroup_path": str(cgroup_path)}
            snapshot = ch_obs.get_vm_runtime_snapshot(vmachine_id="vm-cgroup", state=state)

        self.assertEqual(snapshot["cgroup_memory_max_raw"], "50000000")
        self.assertEqual(snapshot["cgroup_memory_max_bytes"], 50000000)
        self.assertEqual(snapshot["cgroup_memory_current_bytes"], 33000000)


if __name__ == "__main__":
    unittest.main()
