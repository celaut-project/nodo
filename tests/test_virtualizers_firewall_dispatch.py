import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers import firewall as vm_firewall
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    vm_firewall = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VirtualizerFirewallDispatchTests(unittest.TestCase):
    def test_resolve_virtualizer_raises_for_unknown_virtualizer_from_db(self):
        with patch.object(vm_firewall.sc, "get_internal_virtualizer", return_value="lxc"):
            with self.assertRaisesRegex(ValueError, "Unknown virtualizer"):
                vm_firewall._resolve_virtualizer("vm-unknown")

    def test_resolve_virtualizer_raises_for_invalid_default_when_db_empty(self):
        with patch.object(vm_firewall.sc, "get_internal_virtualizer", return_value=None), patch.object(
            vm_firewall.env_manager, "get", return_value="invalid_default"
        ):
            with self.assertRaisesRegex(ValueError, "Unknown virtualizer"):
                vm_firewall._resolve_virtualizer("vm-no-db")

    def test_allow_host_connection_dispatches_to_cloud_hypervisor(self):
        # No `ensure_forward_related_established_rule` patch here: this entry point
        # deliberately does not depend on it, because its rule is on the input hook
        # and carries no conntrack state of its own.
        with patch.object(
            vm_firewall.sc, "get_internal_virtualizer", return_value="cloud_hypervisor"
        ), patch(
            "src.virtualizers.ch.firewall.allow_host_connection",
            return_value=True,
        ) as ch_allow:
            result = vm_firewall.allow_host_connection(
                vmachine_id="vm-123",
                host_ip="192.168.200.1",
                port=5000,
                protocol=vm_firewall.TransportProtocol.TCP,
            )

        self.assertTrue(result)
        ch_allow.assert_called_once_with(
            vmachine_id="vm-123",
            host_ip="192.168.200.1",
            port=5000,
            protocol=vm_firewall.TransportProtocol.TCP,
            source_ip=None,
        )

    def test_allow_connection_to_instance_dispatches_to_cloud_hypervisor_with_source_ip(self):
        with patch.object(
            vm_firewall.sc, "get_internal_virtualizer", return_value="cloud_hypervisor"
        ), patch.object(
            vm_firewall, "ensure_forward_related_established_rule", return_value=True
        ), patch(
            "src.virtualizers.ch.firewall.allow_connection_to_instance",
            return_value=True,
        ) as ch_allow:
            result = vm_firewall.allow_connection_to_instance(
                vmachine_id="vm-123",
                instance=celaut.Instance(),
                source_ip="192.168.200.5",
            )

        self.assertTrue(result)
        ch_allow.assert_called_once_with(
            vmachine_id="vm-123",
            instance=unittest.mock.ANY,
            source_ip="192.168.200.5",
        )

    def test_allow_connection_to_instance_rejects_removed_docker_virtualizer(self):
        with patch.object(vm_firewall.sc, "get_internal_virtualizer", return_value="docker"), patch.object(
            vm_firewall, "ensure_forward_related_established_rule", return_value=True
        ):
            with self.assertRaisesRegex(ValueError, "Unknown virtualizer"):
                vm_firewall.allow_connection_to_instance(
                    vmachine_id="container-123",
                    instance=celaut.Instance(),
                    source_ip="172.17.0.2",
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
