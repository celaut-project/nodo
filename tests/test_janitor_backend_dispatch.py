"""The janitor must judge a VM by the backend that launched it.

Both backends write into the same runtime-state directory, so the janitor sweeps
CH and QEMU guests together. Liveness is not backend-agnostic though: it confirms
the recorded PID still belongs to *this* VM by matching the launcher's visible
process name, and those names differ (``nodo-ch-<id8>`` vs ``nodo-qemu-<id8>``).

Checking a QEMU guest with CH's matcher therefore fails the name test on a
perfectly healthy VM, and the janitor reaps it as ``stale_runtime_process_dead``
seconds after it boots. Observed on a real arm64-on-x86 launch: the guest came up
under TCG, published its endpoint, and was killed before a single request reached
it. The same mismatch applies to teardown, which must also be the launcher's.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.ch import maintain as ch_maintain
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ch_maintain = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class JanitorBackendDispatchTests(unittest.TestCase):
    """A QEMU guest is asked the QEMU questions, a CH guest the CH ones."""

    def _sweep(self, state, *, ch_alive, qemu_alive, in_db=True):
        """Run the janitor with both backends' probes distinguishable.

        Each ``pid_alive`` returns a different answer, so which one the janitor
        consulted is visible in whether the VM survived.
        """
        with patch.object(
            ch_maintain, "list_runtime_states", return_value={"vm-1": state}
        ), patch.object(
            ch_maintain.sc, "internal_instance_exists", return_value=in_db
        ), patch.object(
            ch_maintain, "pid_alive", return_value=ch_alive
        ), patch(
            "src.virtualizers.qemu.process.pid_alive", return_value=qemu_alive
        ), patch.object(
            ch_maintain, "kill_ch_vm", return_value=True
        ) as ch_kill, patch(
            "src.virtualizers.qemu.kill.kill", return_value=True
        ) as qemu_kill:
            ch_maintain.janitor_cleanup_orphans(debug_mode=False)
        return ch_kill, qemu_kill

    def test_a_live_qemu_guest_survives_a_ch_matcher_that_disagrees(self):
        # The regression: CH's matcher says dead (wrong process name), QEMU's
        # says alive. The VM is alive, so nothing may be killed.
        ch_kill, qemu_kill = self._sweep(
            {"pid": 555, "virtualizer": "qemu"}, ch_alive=False, qemu_alive=True
        )
        ch_kill.assert_not_called()
        qemu_kill.assert_not_called()

    def test_a_dead_qemu_guest_is_torn_down_by_the_qemu_backend(self):
        ch_kill, qemu_kill = self._sweep(
            {"pid": 555, "virtualizer": "qemu"}, ch_alive=True, qemu_alive=False
        )
        qemu_kill.assert_called_once_with(vmachine_id="vm-1")
        ch_kill.assert_not_called()

    def test_a_ch_guest_is_unaffected_by_the_qemu_probe(self):
        ch_kill, qemu_kill = self._sweep(
            {"pid": 555, "virtualizer": "ch"}, ch_alive=False, qemu_alive=True
        )
        ch_kill.assert_called_once_with(vmachine_id="vm-1")
        qemu_kill.assert_not_called()

    def test_state_without_a_virtualizer_falls_back_to_ch(self):
        # Pre-existing state files predate the field; CH is the historical owner.
        ch_kill, qemu_kill = self._sweep({"pid": 555}, ch_alive=False, qemu_alive=True)
        ch_kill.assert_called_once_with(vmachine_id="vm-1")
        qemu_kill.assert_not_called()

    def test_the_recorded_virtualizer_is_matched_case_insensitively(self):
        ch_kill, qemu_kill = self._sweep(
            {"pid": 555, "virtualizer": "QEMU"}, ch_alive=False, qemu_alive=True
        )
        ch_kill.assert_not_called()
        qemu_kill.assert_not_called()

    def test_a_booting_qemu_guest_without_a_row_is_left_alone(self):
        # The booting grace has to survive the dispatch: it is computed from the
        # same liveness answer, which must be QEMU's for a QEMU guest.
        ch_kill, qemu_kill = self._sweep(
            {"pid": 555, "virtualizer": "qemu", "booting": True},
            ch_alive=False,
            qemu_alive=True,
            in_db=False,
        )
        ch_kill.assert_not_called()
        qemu_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
