"""The janitor must judge a VM by what its own launcher recorded.

Both hypervisors write into one runtime-state store, so the janitor sweeps CH and
QEMU guests together. Liveness is where that used to go wrong: it confirms the
recorded PID still belongs to *this* VM by matching the launcher's visible
process name, and those names differ (``nodo-ch-<id8>`` vs ``nodo-qemu-<id8>``).

The janitor had only a state file, so it guessed the name -- and guessed CH.
Checking a QEMU guest with CH's name therefore failed on a perfectly healthy VM,
and the janitor reaped it as ``stale_runtime_process_dead`` seconds after it
booted. Observed on a real arm64-on-x86 launch: the guest came up under TCG,
published its endpoint, and was killed before a single request reached it.

The name is now in the state, so there is nothing to guess. What these tests hold
down is that the janitor uses *that* name and no other, and that teardown goes to
the hypervisor the entry names.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.microvm import maintain as microvm_maintain
    from src.virtualizers.microvm import members
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    microvm_maintain = members = None  # type: ignore[assignment]

VM_ID = "abcdef0123456789"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class JanitorBackendDispatchTests(unittest.TestCase):
    def _sweep(self, state, *, running_process_name, in_db=True):
        """Sweep with exactly one process alive on the host, under one name.

        ``pid_matches`` is the real name check; faking it by name rather than by
        answer is what makes "which name did the janitor ask about" observable.
        """
        def matches(pid, process_name):
            return process_name == running_process_name

        with patch.object(
            microvm_maintain, "list_runtime_states", return_value={VM_ID: state}
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=in_db
        ), patch(
            "src.virtualizers.microvm.process.os.kill", return_value=None
        ), patch(
            "src.virtualizers.microvm.process.proc_state", return_value="S"
        ), patch(
            "src.virtualizers.microvm.process.pid_matches", side_effect=matches
        ), patch.object(
            microvm_maintain, "kill_vm", return_value=True
        ) as kill:
            microvm_maintain.sweep_orphans(debug_mode=False)
        return kill

    def _state(self, hypervisor, **extra):
        return {
            "vmachine_id": VM_ID,
            "pid": 555,
            "virtualizer": hypervisor.name,
            "process_name": hypervisor.process_name(VM_ID),
            **extra,
        }

    def test_a_live_qemu_guest_is_not_reaped_for_failing_chs_name_check(self):
        # The regression: the only process on the host is the QEMU one, and the
        # entry says so. Nothing may be killed.
        kill = self._sweep(
            self._state(members.QEMU),
            running_process_name=members.QEMU.process_name(VM_ID),
        )
        kill.assert_not_called()

    def test_a_dead_qemu_guest_is_torn_down_as_a_qemu_guest(self):
        kill = self._sweep(
            self._state(members.QEMU),
            running_process_name=members.CH.process_name(VM_ID),
        )
        kill.assert_called_once_with(members.QEMU, vmachine_id=VM_ID)

    def test_a_dead_ch_guest_is_torn_down_as_a_ch_guest(self):
        kill = self._sweep(
            self._state(members.CH),
            running_process_name=members.QEMU.process_name(VM_ID),
        )
        kill.assert_called_once_with(members.CH, vmachine_id=VM_ID)

    def test_the_recorded_virtualizer_is_matched_case_insensitively(self):
        kill = self._sweep(
            self._state(members.QEMU, virtualizer="QEMU"),
            running_process_name=members.QEMU.process_name(VM_ID),
        )
        kill.assert_not_called()

    def test_a_booting_qemu_guest_without_a_row_is_left_alone(self):
        # The booting grace has to survive on the same liveness answer, which is
        # the one taken from the entry's own recorded name.
        kill = self._sweep(
            self._state(members.QEMU, booting=True),
            running_process_name=members.QEMU.process_name(VM_ID),
            in_db=False,
        )
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
