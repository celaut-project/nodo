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
class VirtualizerInterfaceDispatchTests(unittest.TestCase):
    def test_hotplug_dispatches_to_ch(self):
        req = celaut.ModifyServiceSystemResourcesInput()
        with patch.object(
            vm_interface.sc, "get_internal_virtualizer", return_value="ch"
        ), patch.object(vm_interface, "ch_hotplug", return_value=True) as ch_hotplug:
            result = vm_interface.hotplug(vmachine_id="vm-1", system_requeriments_range=req)

        self.assertTrue(result)
        ch_hotplug.assert_called_once_with(
            vmachine_id="vm-1",
            system_requeriments_range=req,
        )

    def test_kill_dispatches_to_ch(self):
        with patch.object(
            vm_interface.sc, "get_internal_virtualizer", return_value="ch"
        ), patch.object(vm_interface, "ch_kill", return_value=True) as ch_kill:
            result = vm_interface.kill(vmachine_id="vm-2")

        self.assertTrue(result)
        ch_kill.assert_called_once_with(vmachine_id="vm-2")

    def test_maintain_dispatches_to_ch(self):
        callback = lambda vmachine_id: None
        with patch.object(
            vm_interface.sc, "get_internal_virtualizer", return_value="ch"
        ), patch.object(vm_interface, "ch_maintain") as ch_maintain:
            vm_interface.maintain(
                vmachine_id="vm-3",
                debug_mode=True,
                remove_and_penalize=callback,
            )

        ch_maintain.assert_called_once_with(
            vmachine_id="vm-3",
            debug_mode=True,
            remove_and_penalize=callback,
        )

    def test_remove_dispatches_to_ch(self):
        with patch.object(
            vm_interface.sc, "get_internal_virtualizer", return_value="ch"
        ), patch.object(vm_interface, "remove_ch", return_value=True) as ch_remove:
            result = vm_interface.remove(vmachine_id="vm-4")

        self.assertTrue(result)
        ch_remove.assert_called_once_with(vmachine_id="vm-4")

    def test_remove_firewall_rule_allows_ch(self):
        with patch.object(
            vm_interface.sc, "get_internal_virtualizer", return_value="ch"
        ), patch.object(vm_interface, "vm_remove_rule", return_value=True) as vm_remove_rule:
            result = vm_interface.remove_firewall_rule(
                vmachine_id="vm-5",
                ip="1.1.1.1",
                port=53,
                protocol=vm_interface.TransportProtocol.UDP,
            )

        self.assertTrue(result)
        vm_remove_rule.assert_called_once_with(
            vmachine_id="vm-5",
            ip="1.1.1.1",
            port=53,
            protocol=vm_interface.TransportProtocol.UDP,
        )


if __name__ == "__main__":
    unittest.main()
