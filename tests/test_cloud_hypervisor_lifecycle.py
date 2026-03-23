import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.cloud_hypervisor import kill as ch_kill
    from src.virtualizers.cloud_hypervisor import maintain as ch_maintain
    from src.virtualizers.cloud_hypervisor import remove as ch_remove
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ch_kill = None  # type: ignore[assignment]
    ch_maintain = None  # type: ignore[assignment]
    ch_remove = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloudHypervisorLifecycleTests(unittest.TestCase):
    def test_kill_cleans_runtime_resources_idempotently(self):
        state = {
            "pid": 12345,
            "tap": "tapabc",
            "cgroup_path": "/sys/fs/cgroup/nodo-ch/vm-1",
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
            self.assertTrue(runtime_dir.exists())

            with patch.object(ch_kill, "load_runtime_state", return_value=state), patch.object(
                ch_kill, "_runtime_dir", return_value=runtime_dir
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

    def test_remove_dual_mode_runtime_prefers_kill(self):
        with patch.object(
            ch_remove, "CACHE", "/tmp"
        ), patch.object(
            ch_remove, "load_runtime_state", return_value={"pid": 333}
        ), patch.object(
            ch_remove, "kill", return_value=True
        ) as kill_mock:
            result = ch_remove.remove("vm-runtime")

        self.assertTrue(result)
        kill_mock.assert_called_once_with(vmachine_id="vm-runtime")

    def test_remove_dual_mode_bundle_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_dir = Path(tmpdir) / "cloud_hypervisor" / "service-1"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            with patch.object(
                ch_remove, "CACHE", tmpdir
            ), patch.object(
                ch_remove, "load_runtime_state", return_value=None
            ):
                result = ch_remove.remove("service-1")

        self.assertTrue(result)
        self.assertFalse(bundle_dir.exists())


if __name__ == "__main__":
    unittest.main()
