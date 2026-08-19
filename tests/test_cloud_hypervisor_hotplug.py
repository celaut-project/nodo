import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.ch import hotplug as ch_hotplug
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

    def _run(self, req, vmachine_id="vm-r", pid=999):
        """Runs hotplug with every host effect stubbed; returns (result, report, mocks)."""
        persisted = {}

        def _capture_state(_vmachine_id, payload):
            persisted["payload"] = payload

        with ExitStack() as stack:
            def _patch(name, **kwargs):
                return stack.enter_context(patch.object(ch_hotplug, name, **kwargs))

            _patch("load_runtime_state", return_value={"vmachine_id": vmachine_id, "pid": pid})
            _patch("save_runtime_state", side_effect=_capture_state)
            _patch("cgroup_v2_available", return_value=True)
            _patch("ensure_vm_cgroup", return_value=Path(f"/sys/fs/cgroup/nodo-ch/{vmachine_id}"))
            mocks = {
                "apply_memory_limit": _patch("apply_memory_limit"),
                "apply_cpu_limit": _patch("apply_cpu_limit"),
                "modify_sysreq": _patch("modify_sysreq", return_value=True),
            }
            result = ch_hotplug.hotplug(vmachine_id=vmachine_id, system_requeriments_range=req)

        return result, persisted["payload"]["last_hotplug_report"]["results"], mocks

    def test_hotplug_persists_only_the_fields_it_applied(self):
        # disk_space is reported unsupported and nothing here resizes an image, so it
        # must not reach the row -- the tick would then bill an instance for a disk
        # change that never happened.
        req = self._base_request()
        req.max_sysreq.disk_space = 1024

        result, report, mocks = self._run(req, vmachine_id="vm-applied")

        self.assertTrue(result)
        self.assertEqual(report["disk_space"]["status"], "unsupported")
        persisted_sysreq = mocks["modify_sysreq"].call_args.kwargs["sys_req"]
        self.assertFalse(persisted_sysreq.HasField("disk_space"))
        self.assertEqual(persisted_sysreq.mem_limit, req.max_sysreq.mem_limit)
        self.assertEqual(persisted_sysreq.cpu_period, req.max_sysreq.cpu_period)
        self.assertEqual(persisted_sysreq.cpu_quota, req.max_sysreq.cpu_quota)

    def test_a_disk_only_resize_does_not_touch_the_row_at_all(self):
        # Nothing is applied to the guest, so nothing may be written: a request the
        # virtualizer declines in full leaves the row exactly as it was, and with it the
        # price of an instance still holding its whole image.
        req = celaut.ModifyServiceSystemResourcesInput()
        req.max_sysreq.disk_space = 1

        result, report, mocks = self._run(req, vmachine_id="vm-disk-only")

        mocks["modify_sysreq"].assert_not_called()
        self.assertEqual(report["db"]["status"], "ignored")
        self.assertEqual(report["disk_space"]["status"], "unsupported")
        self.assertTrue(result)  # nothing failed; nothing was asked that could be done

    def test_hotplug_refuses_an_unlimited_memory_request(self):
        # mem_limit=0 would write memory.max=max and store 0, which prices as no memory:
        # unbounded RAM for free.
        req = celaut.ModifyServiceSystemResourcesInput()
        req.max_sysreq.mem_limit = 0

        result, report, mocks = self._run(req, vmachine_id="vm-unlimited-mem")

        self.assertFalse(result)
        self.assertEqual(report["mem_limit"]["status"], "unsupported")
        mocks["apply_memory_limit"].assert_not_called()
        mocks["modify_sysreq"].assert_not_called()

    def test_hotplug_refuses_an_unlimited_cpu_request(self):
        req = celaut.ModifyServiceSystemResourcesInput()
        req.max_sysreq.cpu_period = 100000
        req.max_sysreq.cpu_quota = 0

        result, report, mocks = self._run(req, vmachine_id="vm-unlimited-cpu")

        self.assertFalse(result)
        self.assertEqual(report["cpu"]["status"], "unsupported")
        mocks["apply_cpu_limit"].assert_not_called()
        mocks["modify_sysreq"].assert_not_called()

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
