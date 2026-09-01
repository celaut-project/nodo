"""QEMU hotplug: memory via virtio-balloon (QMP), CPU via cgroup, and the
interface dispatch that routes a QEMU instance here instead of to CH.

Unit-level: the QMP client and cgroup writers are patched, so no live VM. The
point is to pin the *contract* the live nodo#274 re-test proved is required --
memory is driven through the balloon and the cgroup ceiling is never shrunk
below the boot allocation (which is what OOM-killed qemu when ch_hotplug owned
memory), while CPU still goes through cgroup cpu.max.
"""
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.qemu import hotplug as qemu_hotplug
    from src.virtualizers import interface
except Exception as import_exc:  # pragma: no cover
    IMPORT_ERROR = import_exc
    celaut = None
    qemu_hotplug = None
    interface = None

BOOT_MEM = 640 * 1024 * 1024


def _mem_cpu_req():
    req = celaut.ModifyServiceSystemResourcesInput()
    req.max_sysreq.mem_limit = 128 * 1024 * 1024  # a shrink, below boot alloc
    req.max_sysreq.cpu_period = 100000
    req.max_sysreq.cpu_quota = 100000
    return req


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class QemuHotplugTests(unittest.TestCase):
    def _run(self, state, req, free=BOOT_MEM - 32 * 1024 * 1024, actual=BOOT_MEM):
        """Run a hotplug against a stand-in guest holding `actual`, `free` of it.

        The defaults are a mostly idle guest, so a shrink is affordable and lands
        verbatim. Pass ``free=None`` for a guest that cannot report.
        """
        persisted = {}

        class _FakeQMP:
            last_target = None
            def __init__(self_, *a, **k): pass
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def set_balloon(self_, target_bytes): _FakeQMP.last_target = int(target_bytes)
            def balloon_actual_bytes(self_): return actual
            def guest_free_bytes(self_): return free

        with patch.object(qemu_hotplug, "load_runtime_state", return_value=state), \
             patch.object(qemu_hotplug, "save_runtime_state", side_effect=lambda vid, p: persisted.update({"vid": vid, "payload": p})), \
             patch.object(qemu_hotplug, "cgroup_v2_available", return_value=True), \
             patch.object(qemu_hotplug, "ensure_vm_cgroup", return_value=Path("/sys/fs/cgroup/nodo-ch/vm-q")) as ecg, \
             patch.object(qemu_hotplug, "apply_memory_limit") as amem, \
             patch.object(qemu_hotplug, "apply_cpu_limit") as acpu, \
             patch.object(qemu_hotplug, "modify_sysreq", return_value=True) as mdb, \
             patch.object(qemu_hotplug, "QMPClient", _FakeQMP):
            ok = qemu_hotplug.hotplug(vmachine_id="vm-q", system_requeriments_range=req)
        return ok, persisted, ecg, amem, acpu, mdb, _FakeQMP

    def test_memory_shrink_uses_balloon_and_holds_cgroup_at_boot_alloc(self):
        state = {"vmachine_id": "vm-q", "pid": 4242, "qmp_socket": "/run/qmp.sock", "boot_mem_bytes": BOOT_MEM}
        ok, persisted, ecg, amem, acpu, mdb, fq = self._run(state, _mem_cpu_req())

        self.assertTrue(ok)
        # Balloon was driven to the requested shrink target, not the cgroup.
        self.assertEqual(fq.last_target, 128 * 1024 * 1024)
        # cgroup memory.max was pinned at the BOOT allocation, never shrunk below
        # it -- the exact thing that OOM-killed qemu when ch_hotplug owned memory.
        amem.assert_called_once()
        self.assertEqual(amem.call_args.kwargs["mem_limit"], BOOT_MEM)
        # CPU still enforced via cgroup cpu.max.
        acpu.assert_called_once()
        report = persisted["payload"]["last_hotplug_report"]["results"]
        self.assertEqual(report["mem_limit"]["status"], "applied")
        self.assertIn("virtio-balloon", report["mem_limit"]["detail"])
        self.assertEqual(report["cpu"]["status"], "applied")

    def test_legacy_instance_without_qmp_falls_back_to_cgroup_best_effort(self):
        # No qmp_socket / boot_mem_bytes: cgroup-only, flagged best-effort.
        state = {"vmachine_id": "vm-q", "pid": 4242}
        req = celaut.ModifyServiceSystemResourcesInput()
        req.max_sysreq.mem_limit = 200 * 1024 * 1024
        ok, persisted, ecg, amem, acpu, mdb, fq = self._run(state, req)
        self.assertTrue(ok)
        self.assertIsNone(fq.last_target)  # balloon NOT used
        amem.assert_called_once()
        self.assertEqual(amem.call_args.kwargs["mem_limit"], 200 * 1024 * 1024)
        detail = persisted["payload"]["last_hotplug_report"]["results"]["mem_limit"]["detail"]
        self.assertIn("best-effort", detail)

    def test_an_unaffordable_shrink_is_clamped_but_still_a_success(self):
        # The guest is using nearly all of its 640 MiB; the 128 MiB request would
        # OOM-panic it. It must be bounded, reported as `clamped` rather than
        # `applied`, and the DB priced at what the guest actually still holds.
        state = {"vmachine_id": "vm-q", "pid": 4242, "qmp_socket": "/run/qmp.sock", "boot_mem_bytes": BOOT_MEM}
        ok, persisted, ecg, amem, acpu, mdb, fq = self._run(
            state, _mem_cpu_req(), free=16 * 1024 * 1024
        )

        self.assertTrue(ok)
        self.assertGreater(fq.last_target, 128 * 1024 * 1024)
        report = persisted["payload"]["last_hotplug_report"]["results"]
        self.assertEqual(report["mem_limit"]["status"], "clamped")
        self.assertEqual(report["mem_limit"]["delivered"], fq.last_target)
        self.assertEqual(mdb.call_args.kwargs["sys_req"].mem_limit, fq.last_target)

    def test_a_guest_that_cannot_report_keeps_the_memory_it_has(self):
        # No statistics => nothing safe to reclaim. Not an error, and not a
        # silent `applied` either.
        state = {"vmachine_id": "vm-q", "pid": 4242, "qmp_socket": "/run/qmp.sock", "boot_mem_bytes": BOOT_MEM}
        ok, persisted, ecg, amem, acpu, mdb, fq = self._run(
            state, _mem_cpu_req(), free=None, actual=None
        )

        self.assertTrue(ok)
        self.assertEqual(fq.last_target, BOOT_MEM)
        report = persisted["payload"]["last_hotplug_report"]["results"]
        self.assertEqual(report["mem_limit"]["status"], "clamped")
        self.assertEqual(mdb.call_args.kwargs["sys_req"].mem_limit, BOOT_MEM)

    def test_invalid_pid_fails(self):
        state = {"vmachine_id": "vm-q", "pid": 0, "qmp_socket": "/run/qmp.sock", "boot_mem_bytes": BOOT_MEM}
        ok, persisted, *_ = self._run(state, _mem_cpu_req())
        self.assertFalse(ok)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InterfaceHotplugDispatchTests(unittest.TestCase):
    def test_qemu_instance_routes_to_qemu_hotplug(self):
        req = celaut.ModifyServiceSystemResourcesInput()
        req.max_sysreq.mem_limit = 128 * 1024 * 1024
        with patch.object(interface, "_resolve_instance_virtualizer", return_value=interface.QEMU), \
             patch.object(interface, "qemu_hotplug", return_value=True) as qh, \
             patch.object(interface, "ch_hotplug", return_value=True) as ch:
            self.assertTrue(interface.hotplug(vmachine_id="vm-q", system_requeriments_range=req))
        qh.assert_called_once()
        ch.assert_not_called()

    def test_ch_instance_still_routes_to_ch_hotplug(self):
        req = celaut.ModifyServiceSystemResourcesInput()
        req.max_sysreq.mem_limit = 128 * 1024 * 1024
        with patch.object(interface, "_resolve_instance_virtualizer", return_value=interface.CH), \
             patch.object(interface, "qemu_hotplug", return_value=True) as qh, \
             patch.object(interface, "ch_hotplug", return_value=True) as ch:
            self.assertTrue(interface.hotplug(vmachine_id="vm-c", system_requeriments_range=req))
        ch.assert_called_once()
        qh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
