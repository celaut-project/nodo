import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.ch import kill as ch_kill
    from src.virtualizers.ch import maintain as ch_maintain
    from src.virtualizers.ch import process as ch_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ch_kill = None  # type: ignore[assignment]
    ch_maintain = None  # type: ignore[assignment]
    ch_process = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloudHypervisorLifecycleTests(unittest.TestCase):
    def test_kill_cleans_runtime_resources_idempotently(self):
        state = {
            "pid": 12345,
            "tap": "tapabc",
            "cgroup_path": "/sys/fs/cgroup/nodo-ch/vm-1",
            "api_socket": "",
            "dnat_rules": [
                {
                    "protocol": "tcp",
                    "external_port": 40000,
                    "internal_port": 8080,
                    "destination_ip": "192.168.200.10",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime-vm"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            socket_dir = Path(tmpdir) / "sockets"
            socket_dir.mkdir(parents=True, exist_ok=True)
            socket_path = socket_dir / "ch-vm-1.sock"
            socket_path.touch()
            self.assertTrue(runtime_dir.exists())
            self.assertTrue(socket_path.exists())

            with patch.object(ch_kill, "load_runtime_state", return_value=state), patch.object(
                ch_kill, "_runtime_dir", return_value=runtime_dir
            ), patch.object(
                ch_kill, "_api_socket_path", return_value=socket_path
            ), patch.object(
                ch_kill,
                "pid_matches_vmachine",
                return_value=True,
            ), patch.object(
                ch_kill.os, "kill", side_effect=ProcessLookupError
            ) as os_kill, patch.object(
                ch_kill, "remove_vm_cgroup"
            ) as remove_vm_cgroup, patch.object(
                ch_kill, "delete_runtime_state"
            ) as delete_runtime_state:
                result = ch_kill.kill(vmachine_id="vm-1")

        self.assertTrue(result)
        os_kill.assert_called_once()
        remove_vm_cgroup.assert_called_once_with(
            vmachine_id="vm-1",
            cgroup_path="/sys/fs/cgroup/nodo-ch/vm-1",
        )
        delete_runtime_state.assert_called_once_with("vm-1")
        self.assertFalse(runtime_dir.exists())
        self.assertFalse(socket_path.exists())

    def test_kill_does_not_signal_reused_pid(self):
        state = {"pid": 12345, "tap": "", "api_socket": "", "cleanup_rules": []}
        with patch.object(ch_kill, "load_runtime_state", return_value=state), patch.object(
            ch_kill,
            "pid_matches_vmachine",
            return_value=False,
        ), patch.object(
            ch_kill.os,
            "kill",
        ) as os_kill, patch.object(
            ch_kill,
            "remove_vm_cgroup",
        ), patch.object(
            ch_kill,
            "delete_runtime_state",
        ):
            result = ch_kill.kill(vmachine_id="vm-reused")

        self.assertTrue(result)
        os_kill.assert_not_called()

    def test_maintain_penalizes_when_state_or_pid_invalid(self):
        removed = []

        def _remove(vmachine_id):
            removed.append(vmachine_id)

        with patch.object(ch_maintain, "load_runtime_state", return_value=None):
            ch_maintain.maintain(
                vmachine_id="vm-missing",
                debug_mode=True,
                remove_and_penalize=_remove,
            )
        self.assertEqual(removed, ["vm-missing"])

        removed.clear()
        with patch.object(ch_maintain, "load_runtime_state", return_value={"pid": 0}):
            ch_maintain.maintain(
                vmachine_id="vm-invalid-pid",
                debug_mode=True,
                remove_and_penalize=_remove,
            )
        self.assertEqual(removed, ["vm-invalid-pid"])

    def test_maintain_penalizes_when_socket_missing(self):
        removed = []

        def _remove(vmachine_id):
            removed.append(vmachine_id)

        with patch.object(
            ch_maintain,
            "load_runtime_state",
            return_value={"pid": 222, "api_socket": "/tmp/not-found.sock"},
        ), patch.object(
            ch_maintain.os, "kill", return_value=None
        ), patch.object(
            ch_maintain.os.path, "exists", return_value=False
        ):
            ch_maintain.maintain(
                vmachine_id="vm-socket",
                debug_mode=True,
                remove_and_penalize=_remove,
            )

        self.assertEqual(removed, ["vm-socket"])

    def test_pid_alive_rejects_zombie_process(self):
        with patch.object(ch_process.os, "kill", return_value=None), patch.object(
            ch_process,
            "proc_state",
            return_value="Z",
        ), patch.object(
            ch_process,
            "pid_matches_vmachine",
            return_value=True,
        ) as pid_matches:
            alive = ch_process.pid_alive(pid=222, vmachine_id="vm-zombie")

        self.assertFalse(alive)
        pid_matches.assert_not_called()

    def test_pid_alive_rejects_reused_pid_for_another_process(self):
        with patch.object(ch_process.os, "kill", return_value=None), patch.object(
            ch_process,
            "proc_state",
            return_value="S",
        ), patch.object(
            ch_process,
            "pid_matches_vmachine",
            return_value=False,
        ):
            alive = ch_process.pid_alive(pid=222, vmachine_id="vm-reused")

        self.assertFalse(alive)

    def test_janitor_cleans_orphan_runtime(self):
        with patch.object(
            ch_maintain,
            "list_runtime_states",
            return_value={"vm-orphan": {"pid": 777}},
        ), patch.object(
            ch_maintain.sc,
            "internal_instance_exists",
            return_value=False,
        ), patch.object(
            ch_maintain,
            "kill_ch_vm",
            return_value=True,
        ) as kill_mock:
            ch_maintain.janitor_cleanup_orphans(debug_mode=False)

        kill_mock.assert_called_once_with(vmachine_id="vm-orphan")

    def test_janitor_cleans_stale_dead_process(self):
        with patch.object(
            ch_maintain,
            "list_runtime_states",
            return_value={"vm-dead": {"pid": 888}},
        ), patch.object(
            ch_maintain.sc,
            "internal_instance_exists",
            return_value=True,
        ), patch.object(
            ch_maintain,
            "pid_alive",
            return_value=False,
        ), patch.object(
            ch_maintain,
            "kill_ch_vm",
            return_value=True,
        ) as kill_mock:
            ch_maintain.janitor_cleanup_orphans(debug_mode=False)

        kill_mock.assert_called_once_with(vmachine_id="vm-dead")

    def test_janitor_skips_healthy_registered_runtime(self):
        with patch.object(
            ch_maintain,
            "list_runtime_states",
            return_value={"vm-ok": {"pid": 999}},
        ), patch.object(
            ch_maintain.sc,
            "internal_instance_exists",
            return_value=True,
        ), patch.object(
            ch_maintain,
            "pid_alive",
            return_value=True,
        ), patch.object(
            ch_maintain,
            "kill_ch_vm",
            return_value=True,
        ) as kill_mock:
            ch_maintain.janitor_cleanup_orphans(debug_mode=False)

        kill_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
