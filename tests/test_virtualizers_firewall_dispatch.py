"""The firewall frontend reaches the family's implementation, and nothing else.

The three tests that used to sit alongside these exercised
``_resolve_virtualizer`` -- a database lookup whose answer all six call sites
discarded, since every branch resolved to the same implementation. It is gone,
and with it the tests that made its dead half look alive. What is worth holding
down is what these entry points actually promise: the arguments they pass on, and
which of them depend on the global return-traffic accept.
"""
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
class VirtualizerFirewallFrontendTests(unittest.TestCase):
    def test_allow_host_connection_needs_no_return_traffic_accept(self):
        # Deliberately no `ensure_forward_related_established_rule` patch: this
        # entry point does not depend on it, because its rule is on the input hook
        # and carries no conntrack state of its own.
        with patch(
            "src.virtualizers.microvm.firewall.allow_host_connection",
            return_value=True,
        ) as fw_allow:
            result = vm_firewall.allow_host_connection(
                vmachine_id="vm-123",
                host_ip="192.168.200.1",
                port=5000,
                protocol=vm_firewall.TransportProtocol.TCP,
            )

        self.assertTrue(result)
        fw_allow.assert_called_once_with(
            vmachine_id="vm-123",
            host_ip="192.168.200.1",
            port=5000,
            protocol=vm_firewall.TransportProtocol.TCP,
            source_ip=None,
        )

    def test_allow_connection_to_instance_carries_the_source_ip(self):
        with patch.object(
            vm_firewall, "ensure_forward_related_established_rule", return_value=True
        ), patch(
            "src.virtualizers.microvm.firewall.allow_connection_to_instance",
            return_value=True,
        ) as fw_allow:
            result = vm_firewall.allow_connection_to_instance(
                vmachine_id="vm-123",
                instance=celaut.Instance(),
                source_ip="192.168.200.5",
            )

        self.assertTrue(result)
        fw_allow.assert_called_once_with(
            vmachine_id="vm-123",
            instance=unittest.mock.ANY,
            source_ip="192.168.200.5",
        )

    def test_a_forward_side_allow_is_refused_without_the_return_accept(self):
        # Writing an allow into FORWARD while return traffic is still evaluated
        # against the blanket drop is a rule that grants nothing.
        with patch.object(
            vm_firewall, "ensure_forward_related_established_rule", return_value=False
        ), patch(
            "src.virtualizers.microvm.firewall.allow_connection_to_instance"
        ) as fw_allow:
            result = vm_firewall.allow_connection_to_instance(
                vmachine_id="vm-123",
                instance=celaut.Instance(),
                source_ip="192.168.200.5",
            )

        self.assertFalse(result)
        fw_allow.assert_not_called()

    def test_remove_rule_passes_the_target_through(self):
        with patch(
            "src.virtualizers.microvm.firewall.remove_rule",
            return_value=True,
        ) as fw_remove:
            result = vm_firewall.remove_rule(
                vmachine_id="vm-456",
                ip="1.1.1.1",
                port=53,
                protocol=vm_firewall.TransportProtocol.UDP,
            )

        self.assertTrue(result)
        fw_remove.assert_called_once_with(
            vmachine_id="vm-456",
            ip="1.1.1.1",
            port=53,
            protocol=vm_firewall.TransportProtocol.UDP,
        )

    def test_remove_vm_rules_needs_no_database_lookup(self):
        # It used to resolve the instance's backend first, which meant a teardown
        # could not drop a VM's rules once its row was gone.
        with patch(
            "src.virtualizers.microvm.firewall.remove_vm_rules", return_value=3
        ) as fw_remove:
            self.assertEqual(vm_firewall.remove_vm_rules(vmachine_id="vm-789"), 3)

        fw_remove.assert_called_once_with(vmachine_id="vm-789")


if __name__ == "__main__":
    unittest.main()
