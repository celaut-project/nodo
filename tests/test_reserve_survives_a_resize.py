"""The guest kernel reserve has to survive a resize, not just a boot.

Booting a guest larger than its manifest declared is only half the promise. The
figures the node hands a hypervisor after launch -- a balloon target, a cgroup
``memory.max`` -- are *guest allocations*, while a resize request arrives in the
same unit a manifest and a row are written in: bytes the **service** may use. The
guest kernel's footprint sits between the two, and it does not shrink when the
balloon inflates: the kernel sized its ``struct page`` array for the whole of
``-m`` at boot and keeps it.

So a resize that treats a usable target as an allocation reintroduces exactly the
shortfall the reserve exists to close, one step later than the boot path:

* QEMU -- a grow to the declared ceiling would set the balloon to that figure and
  leave the service that much *minus a kernel*, which is the bug the celaut
  demo-service's `memory_ceiling` probe reported as DISHONEST.
* CH -- its resize knob *is* the cgroup, so capping ``memory.max`` at the usable
  figure would also put the cap below RAM the VM already has mapped.

The reserve is therefore measured once at boot, persisted in the instance's
runtime state, and added back by both backends' resize paths. Read from the state
rather than recomputed on purpose: an operator who edits the reserve must not move
the arithmetic of guests already running under the old figure.

An instance whose runtime state carries no such key was booted at exactly its usable
figure. Its reserve reads as zero and every figure below collapses to the plain
usable target, so a node can pick up guests launched without one.
"""
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.ch import hotplug as ch_hotplug
    from src.virtualizers.qemu import hotplug as qemu_hotplug
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    ch_hotplug = None  # type: ignore[assignment]
    qemu_hotplug = None  # type: ignore[assignment]

MIB = 1024 * 1024

# A guest booted for a 512 MiB usable ceiling on amd64, with the reserve its runtime
# state records: the arch's fixed part plus a share of the guest.
USABLE_CEILING = 512 * MIB
RESERVE = 40 * MIB + 26 * MIB
BOOT_MEM = USABLE_CEILING + RESERVE


def _mem_request(mem_limit):
    req = celaut.ModifyServiceSystemResourcesInput()
    req.max_sysreq.mem_limit = mem_limit
    return req


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheQemuBalloonSpeaksAllocationsTests(unittest.TestCase):
    """A balloon target is an allocation; the request that drives it is not."""

    def _run(self, state, req, free=BOOT_MEM - 32 * MIB, actual=BOOT_MEM):
        """Hotplug against a stand-in guest holding `actual`, `free` of it idle.

        Idle enough that a shrink is affordable and lands verbatim, so what the
        guest is asked for is the figure under test rather than a safety clamp.
        """

        class _FakeQMP:
            last_target = None

            def __init__(self_, *a, **k):
                pass

            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def set_balloon(self_, target_bytes):
                _FakeQMP.last_target = int(target_bytes)

            def balloon_actual_bytes(self_):
                return actual

            def guest_free_bytes(self_):
                return free

        with patch.object(qemu_hotplug, "load_runtime_state", return_value=state), \
             patch.object(qemu_hotplug, "save_runtime_state"), \
             patch.object(qemu_hotplug, "cgroup_v2_available", return_value=True), \
             patch.object(qemu_hotplug, "ensure_vm_cgroup", return_value=Path("/sys/fs/cgroup/nodo-ch/vm-q")), \
             patch.object(qemu_hotplug, "apply_memory_limit") as apply_mem, \
             patch.object(qemu_hotplug, "modify_sysreq", return_value=True), \
             patch.object(qemu_hotplug, "QMPClient", _FakeQMP):
            ok = qemu_hotplug.hotplug(vmachine_id="vm-q", system_requeriments_range=req)
        return ok, _FakeQMP.last_target, apply_mem

    def _state(self, **extra):
        state = {
            "vmachine_id": "vm-q",
            "pid": 4242,
            "qmp_socket": "/run/qmp.sock",
            "boot_mem_bytes": BOOT_MEM,
            "guest_kernel_reserve_bytes": RESERVE,
        }
        state.update(extra)
        return state

    def test_a_shrink_leaves_the_service_the_bytes_it_asked_for(self):
        # 128 MiB usable means a 128 MiB + reserve allocation. Asking the guest for
        # 128 MiB flat would leave the service 128 minus a kernel.
        ok, target, _ = self._run(self._state(), _mem_request(128 * MIB))
        self.assertTrue(ok)
        self.assertEqual(target, 128 * MIB + RESERVE)

    def test_a_grow_to_the_declared_ceiling_is_reachable_in_full(self):
        # The whole point of booting at the ceiling: a grow back to it must land,
        # and must land on the *allocation* that makes the ceiling usable -- which
        # is exactly the boot allocation, so it is affordable by construction.
        ok, target, _ = self._run(self._state(), _mem_request(USABLE_CEILING))
        self.assertTrue(ok)
        self.assertEqual(target, BOOT_MEM)

    def test_the_cgroup_is_never_moved_off_the_boot_allocation(self):
        # Unchanged by the reserve, and worth pinning here too: memory.max below
        # what qemu has mapped is what OOM-kills it (nodo#274).
        _, _, apply_mem = self._run(self._state(), _mem_request(128 * MIB))
        apply_mem.assert_called_once()
        self.assertEqual(apply_mem.call_args.kwargs["mem_limit"], BOOT_MEM)

    def test_a_request_the_boot_allocation_cannot_cover_is_clamped_not_refused(self):
        # A usable figure above the declared ceiling needs more than `-m`, which
        # QEMU cannot give. Clamped to the boot allocation and reported, rather
        # than handed to the guest as a target it cannot mean anything by.
        ok, target, _ = self._run(self._state(), _mem_request(4096 * MIB))
        self.assertTrue(ok)
        self.assertEqual(target, BOOT_MEM)

    def test_what_is_reported_back_stays_in_usable_bytes(self):
        # The unit the request arrived in and the unit the row is priced in. A
        # report in allocation bytes would have the caller record a figure that
        # includes the kernel the node is absorbing.
        req = _mem_request(128 * MIB)
        state = self._state()
        captured = {}

        with patch.object(qemu_hotplug, "load_runtime_state", return_value=state), \
             patch.object(qemu_hotplug, "save_runtime_state", side_effect=lambda vid, p: captured.update(p)), \
             patch.object(qemu_hotplug, "cgroup_v2_available", return_value=True), \
             patch.object(qemu_hotplug, "ensure_vm_cgroup", return_value=Path("/sys/fs/cgroup/nodo-ch/vm-q")), \
             patch.object(qemu_hotplug, "apply_memory_limit"), \
             patch.object(qemu_hotplug, "modify_sysreq", return_value=True), \
             patch.object(qemu_hotplug, "QMPClient", self._idle_guest()):
            qemu_hotplug.hotplug(vmachine_id="vm-q", system_requeriments_range=req)

        report = captured.get("last_hotplug_report") or {}
        self.assertEqual(report["results"]["mem_limit"]["requested"], 128 * MIB)

    def _idle_guest(self):
        class _FakeQMP:
            def __init__(self_, *a, **k):
                pass

            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def set_balloon(self_, target_bytes):
                pass

            def balloon_actual_bytes(self_):
                return BOOT_MEM

            def guest_free_bytes(self_):
                return BOOT_MEM - 32 * MIB

        return _FakeQMP

    def test_an_instance_with_no_recorded_reserve_takes_the_target_verbatim(self):
        # No `guest_kernel_reserve_bytes` in its state: it was booted at its usable
        # figure, so there is nothing to add back and the target is verbatim.
        state = self._state()
        del state["guest_kernel_reserve_bytes"]
        ok, target, _ = self._run(state, _mem_request(128 * MIB))
        self.assertTrue(ok)
        self.assertEqual(target, 128 * MIB)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheChCgroupBoundsTheBootAllocationTests(unittest.TestCase):
    """CH resizes by moving the cgroup, so the cgroup is where the reserve lands."""

    def _run(self, state, mem_limit):
        req = _mem_request(mem_limit)
        req.max_sysreq.cpu_period = 100000
        req.max_sysreq.cpu_quota = 50000

        with patch.object(ch_hotplug, "load_runtime_state", return_value=state), \
             patch.object(ch_hotplug, "save_runtime_state"), \
             patch.object(ch_hotplug, "cgroup_v2_available", return_value=True), \
             patch.object(ch_hotplug, "ensure_vm_cgroup", return_value=Path("/sys/fs/cgroup/nodo-ch/vm-1")), \
             patch.object(ch_hotplug, "apply_memory_limit") as apply_mem, \
             patch.object(ch_hotplug, "apply_cpu_limit"), \
             patch.object(ch_hotplug, "modify_sysreq", return_value=True):
            ok = ch_hotplug.hotplug(vmachine_id="vm-1", system_requeriments_range=req)
        return ok, apply_mem

    def test_memory_max_covers_the_usable_figure_plus_the_reserve(self):
        ok, apply_mem = self._run(
            {"vmachine_id": "vm-1", "pid": 111, "guest_kernel_reserve_bytes": RESERVE},
            256 * MIB,
        )
        self.assertTrue(ok)
        self.assertEqual(apply_mem.call_args.kwargs["mem_limit"], 256 * MIB + RESERVE)

    def test_an_instance_with_no_recorded_reserve_is_capped_at_its_usable_figure(self):
        ok, apply_mem = self._run({"vmachine_id": "vm-1", "pid": 111}, 256 * MIB)
        self.assertTrue(ok)
        self.assertEqual(apply_mem.call_args.kwargs["mem_limit"], 256 * MIB)

    def test_the_reported_figure_is_what_the_service_may_use(self):
        # `requested` is what the row is reconciled against, so it has to stay the
        # usable figure even though memory.max was set higher.
        captured = {}
        req = _mem_request(256 * MIB)


        with patch.object(ch_hotplug, "load_runtime_state", return_value={
                "vmachine_id": "vm-1", "pid": 111, "guest_kernel_reserve_bytes": RESERVE}), \
             patch.object(ch_hotplug, "save_runtime_state", side_effect=lambda vid, p: captured.update(p)), \
             patch.object(ch_hotplug, "cgroup_v2_available", return_value=True), \
             patch.object(ch_hotplug, "ensure_vm_cgroup", return_value=Path("/sys/fs/cgroup/nodo-ch/vm-1")), \
             patch.object(ch_hotplug, "apply_memory_limit"), \
             patch.object(ch_hotplug, "apply_cpu_limit"), \
             patch.object(ch_hotplug, "modify_sysreq", return_value=True):
            ch_hotplug.hotplug(vmachine_id="vm-1", system_requeriments_range=req)

        report = captured.get("last_hotplug_report") or {}
        self.assertEqual(report["results"]["mem_limit"]["requested"], 256 * MIB)


if __name__ == "__main__":
    unittest.main()
