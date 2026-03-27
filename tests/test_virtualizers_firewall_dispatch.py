import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers import firewall as vm_firewall
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    vm_firewall = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VirtualizerFirewallDispatchTests(unittest.TestCase):
    def test_allow_connection_dispatches_to_cloud_hypervisor(self):
        with patch.object(
            vm_firewall.sc, "get_internal_virtualizer", return_value="cloud_hypervisor"
        ), patch(
            "src.virtualizers.ch.firewall.allow_connection",
            return_value=True,
        ) as ch_allow:
            result = vm_firewall.allow_connection(
                vmachine_id="vm-123",
                ip="192.168.200.10",
                port=5000,
                protocol=vm_firewall.TransportProtocol.TCP,
            )

        self.assertTrue(result)
        ch_allow.assert_called_once_with(
            vmachine_id="vm-123",
            ip="192.168.200.10",
            port=5000,
            protocol=vm_firewall.TransportProtocol.TCP,
        )

    def test_allow_connection_dispatches_to_docker(self):
        with patch.object(vm_firewall.sc, "get_internal_virtualizer", return_value="docker"), patch(
            "src.virtualizers.docker.firewall.allow_connection",
            return_value=True,
        ) as docker_allow:
            result = vm_firewall.allow_connection(
                vmachine_id="container-123",
                ip="10.0.0.5",
                port=8080,
                protocol=vm_firewall.TransportProtocol.TCP,
            )

        self.assertTrue(result)
        docker_allow.assert_called_once_with(
            container_id="container-123",
            ip="10.0.0.5",
            port=8080,
            protocol=vm_firewall.TransportProtocol.TCP,
        )

    def test_remove_rule_dispatches_to_cloud_hypervisor(self):
        with patch.object(
            vm_firewall.sc, "get_internal_virtualizer", return_value="cloud_hypervisor"
        ), patch(
            "src.virtualizers.ch.firewall.remove_rule",
            return_value=True,
        ) as ch_remove:
            result = vm_firewall.remove_rule(
                vmachine_id="vm-456",
                ip="1.1.1.1",
                port=53,
                protocol=vm_firewall.TransportProtocol.UDP,
            )

        self.assertTrue(result)
        ch_remove.assert_called_once_with(
            vmachine_id="vm-456",
            ip="1.1.1.1",
            port=53,
            protocol=vm_firewall.TransportProtocol.UDP,
        )


if __name__ == "__main__":
    unittest.main()
