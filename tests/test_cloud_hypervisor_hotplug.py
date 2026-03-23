import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.cloud_hypervisor import hotplug as ch_hotplug
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    ch_hotplug = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloudHypervisorHotplugTests(unittest.TestCase):
    def _base_request(self):
        req = celaut.ModifyServiceSystemResourcesInput()
        req.max_sysreq.mem_limit = 128 * 1024 * 1024
        req.max_sysreq.cpu_period = 100000
        req.max_sysreq.cpu_quota = 50000
        return req

    def test_hotplug_applies_mem_and_cpu_and_reports_unsupported(self):
        req = self._base_request()
        req.max_sysreq.blkio_weight = 100
        req.max_sysreq.disk_space = 1024

        persisted = {}

        def _capture_state(vmachine_id, payload):
            persisted["vmachine_id"] = vmachine_id
            persisted["payload"] = payload

        with patch.object(
            ch_hotplug,
            "load_runtime_state",
            return_value={"vmachine_id": "vm-1", "pid": 111},
        ), patch.object(
            ch_hotplug,
            "save_runtime_state",
            side_effect=_capture_state,
        ), patch.object(
            ch_hotplug,
            "cgroup_v2_available",
            return_value=True,
        ), patch.object(
            ch_hotplug,
            "ensure_vm_cgroup",
            return_value=Path("/sys/fs/cgroup/nodo-ch/vm-1"),
        ) as ensure_vm_cgroup, patch.object(
            ch_hotplug,
            "apply_memory_limit",
        ) as apply_memory_limit, patch.object(
            ch_hotplug,
            "apply_cpu_limit",
        ) as apply_cpu_limit, patch.object(
            ch_hotplug,
            "modify_sysreq",
            return_value=True,
        ) as modify_sysreq:
            result = ch_hotplug.hotplug(vmachine_id="vm-1", system_requeriments_range=req)

        self.assertTrue(result)
        ensure_vm_cgroup.assert_called_once_with(vmachine_id="vm-1", pid=111)
        apply_memory_limit.assert_called_once()
        apply_cpu_limit.assert_called_once()
        modify_sysreq.assert_called_once()
        self.assertEqual(persisted["vmachine_id"], "vm-1")
        payload = persisted["payload"]
        self.assertEqual(payload["cgroup_path"], "/sys/fs/cgroup/nodo-ch/vm-1")
        report = payload["last_hotplug_report"]["results"]
        self.assertEqual(report["mem_limit"]["status"], "applied")
        self.assertEqual(report["cpu"]["status"], "applied")
        self.assertEqual(report["blkio_weight"]["status"], "unsupported")
        self.assertEqual(report["disk_space"]["status"], "unsupported")

    def test_hotplug_fails_strictly_when_supported_field_fails(self):
        req = self._base_request()

        persisted = {}

        def _capture_state(vmachine_id, payload):
            persisted["vmachine_id"] = vmachine_id
            persisted["payload"] = payload

        with patch.object(
            ch_hotplug,
            "load_runtime_state",
            return_value={"vmachine_id": "vm-2", "pid": 222},
        ), patch.object(
            ch_hotplug,
            "save_runtime_state",
            side_effect=_capture_state,
        ), patch.object(
            ch_hotplug,
            "ensure_vm_cgroup",
            return_value=Path("/sys/fs/cgroup/nodo-ch/vm-2"),
        ), patch.object(
            ch_hotplug,
            "apply_memory_limit",
            side_effect=RuntimeError("memory write failed"),
        ), patch.object(
            ch_hotplug,
            "apply_cpu_limit",
        ), patch.object(
            ch_hotplug,
            "modify_sysreq",
            return_value=True,
        ) as modify_sysreq:
            result = ch_hotplug.hotplug(vmachine_id="vm-2", system_requeriments_range=req)

        self.assertFalse(result)
        modify_sysreq.assert_not_called()
        report = persisted["payload"]["last_hotplug_report"]["results"]
        self.assertEqual(report["mem_limit"]["status"], "failed")
        self.assertEqual(report["db"]["status"], "ignored")

    def test_hotplug_requires_valid_pid_for_supported_fields(self):
        req = self._base_request()
        with patch.object(
            ch_hotplug,
            "load_runtime_state",
            return_value={"vmachine_id": "vm-3", "pid": 0},
        ), patch.object(ch_hotplug, "save_runtime_state") as save_runtime_state:
            result = ch_hotplug.hotplug(vmachine_id="vm-3", system_requeriments_range=req)

        self.assertFalse(result)
        save_runtime_state.assert_called_once()


if __name__ == "__main__":
    unittest.main()
