"""The janitor and the launcher must not race for a VM that is still booting.

`execute` records a VM in two places the instant its hypervisor process exists:
the runtime state file first, then the instance row (so a guest that calls the
node during boot can be identified by its address). Between those two writes a
live VM legitimately has a runtime state and no row -- which is exactly the shape
the janitor kills as an orphan. It has to leave that one alone, without losing its
grip on a VM whose process really is gone.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.microvm import maintain as microvm_maintain
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    microvm_maintain = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class JanitorBootingGraceTests(unittest.TestCase):
    def _run_janitor(self, state, in_db, alive):
        state = {"virtualizer": "ch", "process_name": "nodo-ch-vm000001", **state}
        with patch.object(
            microvm_maintain, "list_runtime_states", return_value={"vm-1": state}
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=in_db
        ), patch.object(
            microvm_maintain, "pid_alive", return_value=alive
        ), patch.object(
            microvm_maintain, "kill_vm", return_value=True
        ) as kill_mock:
            microvm_maintain.sweep_orphans(debug_mode=False)
        return kill_mock

    def test_a_booting_vm_without_a_row_is_left_alone(self):
        kill_mock = self._run_janitor(
            {"pid": 555, "booting": True}, in_db=False, alive=True
        )
        kill_mock.assert_not_called()

    def test_a_booting_vm_whose_process_is_gone_is_still_cleaned(self):
        # The grace covers a VM that is running, not a failed boot.
        kill_mock = self._run_janitor(
            {"pid": 555, "booting": True}, in_db=False, alive=False
        )
        kill_mock.assert_called_once_with(
            microvm_maintain.member("ch"), vmachine_id="vm-1"
        )

    def test_a_ready_vm_without_a_row_is_still_an_orphan(self):
        # `booting` is gone from the state once the launch completes, so the
        # ordinary orphan rule applies again.
        kill_mock = self._run_janitor({"pid": 555}, in_db=False, alive=True)
        kill_mock.assert_called_once_with(
            microvm_maintain.member("ch"), vmachine_id="vm-1"
        )

    def test_a_registered_and_running_vm_is_never_touched(self):
        kill_mock = self._run_janitor({"pid": 555}, in_db=True, alive=True)
        kill_mock.assert_not_called()

    def test_an_entry_no_hypervisor_claims_is_reported_not_guessed(self):
        # Defaulting an unrecognized `virtualizer` to CH is what reaped a healthy
        # QEMU guest with CH's process matcher (#295). An entry nobody claims is
        # left alone and logged.
        with patch.object(
            microvm_maintain,
            "list_runtime_states",
            return_value={"vm-1": {"pid": 555, "virtualizer": "firecracker"}},
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            microvm_maintain, "kill_vm", return_value=True
        ) as kill_mock:
            microvm_maintain.sweep_orphans(debug_mode=False)
        kill_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
