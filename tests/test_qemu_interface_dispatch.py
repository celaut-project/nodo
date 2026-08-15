"""interface.execute picks the backend by service arch; kill/maintain route by
the recorded virtualizer column."""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers import interface as vm_interface
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    vm_interface = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InterfaceExecuteDispatchTests(unittest.TestCase):
    def _execute(self):
        return vm_interface.execute(
            assigment_ports=None,
            by_local=True,
            service_id="svc",
            service=celaut.Service(),
            config=None,
            initial_system_resources=celaut.Sysresources(),
            father_id="",
        )

    def test_execute_native_uses_ch(self):
        with patch.object(vm_interface, "select_virtualizer", return_value=vm_interface.CH), \
             patch.object(vm_interface, "ch_execute", return_value=("vm", "ip", None)) as ch, \
             patch.object(vm_interface, "qemu_execute") as qemu:
            self._execute()
        ch.assert_called_once()
        qemu.assert_not_called()

    def test_execute_cross_arch_uses_qemu(self):
        with patch.object(vm_interface, "select_virtualizer", return_value=vm_interface.QEMU), \
             patch.object(vm_interface, "qemu_execute", return_value=("vm", "ip", None)) as qemu, \
             patch.object(vm_interface, "ch_execute") as ch:
            self._execute()
        qemu.assert_called_once()
        ch.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InterfaceLifecycleDispatchTests(unittest.TestCase):
    def test_kill_routes_qemu_by_db_column(self):
        with patch.object(vm_interface.sc, "get_internal_virtualizer", return_value="qemu"), \
             patch.object(vm_interface, "qemu_kill", return_value=True) as qemu_kill, \
             patch.object(vm_interface, "ch_kill") as ch_kill:
            self.assertTrue(vm_interface.kill(vmachine_id="vm-q"))
        qemu_kill.assert_called_once_with(vmachine_id="vm-q")
        ch_kill.assert_not_called()

    def test_kill_routes_ch_by_default(self):
        with patch.object(vm_interface.sc, "get_internal_virtualizer", return_value="ch"), \
             patch.object(vm_interface, "ch_kill", return_value=True) as ch_kill, \
             patch.object(vm_interface, "qemu_kill") as qemu_kill:
            self.assertTrue(vm_interface.kill(vmachine_id="vm-c"))
        ch_kill.assert_called_once_with(vmachine_id="vm-c")
        qemu_kill.assert_not_called()

    def test_kill_defaults_to_ch_on_unknown(self):
        with patch.object(vm_interface.sc, "get_internal_virtualizer", return_value=None), \
             patch.object(vm_interface, "ch_kill", return_value=True) as ch_kill, \
             patch.object(vm_interface, "qemu_kill") as qemu_kill:
            self.assertTrue(vm_interface.kill(vmachine_id="vm-u"))
        ch_kill.assert_called_once()
        qemu_kill.assert_not_called()

    def test_maintain_routes_qemu_by_db_column(self):
        cb = lambda vmachine_id: None
        with patch.object(vm_interface.sc, "get_internal_virtualizer", return_value="qemu"), \
             patch.object(vm_interface, "qemu_maintain") as qemu_maintain, \
             patch.object(vm_interface, "ch_maintain") as ch_maintain:
            vm_interface.maintain(vmachine_id="vm-q", debug_mode=False, remove_and_penalize=cb)
        qemu_maintain.assert_called_once()
        ch_maintain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
