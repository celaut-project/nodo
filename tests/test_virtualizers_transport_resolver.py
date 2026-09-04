import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers import firewall as vm_firewall
    from src.virtualizers.microvm import firewall as ch_firewall
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    vm_firewall = None  # type: ignore[assignment]
    ch_firewall = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TransportResolverTests(unittest.TestCase):
    def test_missing_transport_is_strict_error(self):
        slot = celaut.Service.Api.Slot(port=8080)
        with self.assertRaisesRegex(ValueError, "missing required transport"):
            vm_firewall.resolve_slot_transport_protocols(slot, context="[TEST]")

    def test_tcp_transport_is_resolved_case_insensitive(self):
        slot = celaut.Service.Api.Slot(
            port=8080,
            transport=celaut.Service.Api.Protocol(tags=["TCP"]),
        )
        protocol = vm_firewall.resolve_slot_transport_protocols(slot, context="[TEST]")
        self.assertEqual(protocol, vm_firewall.TransportProtocol.TCP)

    def test_udp_transport_is_resolved(self):
        slot = celaut.Service.Api.Slot(
            port=8080,
            transport=celaut.Service.Api.Protocol(tags=["udp"]),
        )
        protocol = vm_firewall.resolve_slot_transport_protocols(slot, context="[TEST]")
        self.assertEqual(protocol, vm_firewall.TransportProtocol.UDP)

    def test_unsupported_transport_is_ignored(self):
        slot = celaut.Service.Api.Slot(
            port=8080,
            transport=celaut.Service.Api.Protocol(tags=["sctp"]),
        )
        messages = []
        protocol = vm_firewall.resolve_slot_transport_protocols(
            slot,
            logger_fn=messages.append,
            context="[TEST]",
        )
        self.assertIsNone(protocol)
        self.assertTrue(any("unsupported" in message.lower() for message in messages))

    def test_mixed_supported_and_unsupported_transport_keeps_supported(self):
        slot = celaut.Service.Api.Slot(
            port=8080,
            transport=celaut.Service.Api.Protocol(tags=["tcp", "sctp"]),
        )
        messages = []
        protocol = vm_firewall.resolve_slot_transport_protocols(
            slot,
            logger_fn=messages.append,
            context="[TEST]",
        )
        self.assertEqual(protocol, vm_firewall.TransportProtocol.TCP)
        self.assertTrue(any("unsupported" in message.lower() for message in messages))

    def test_multiple_supported_transport_families_is_error(self):
        slot = celaut.Service.Api.Slot(
            port=8080,
            transport=celaut.Service.Api.Protocol(tags=["tcp", "udp"]),
        )
        with self.assertRaisesRegex(ValueError, "single transport"):
            vm_firewall.resolve_slot_transport_protocols(slot, context="[TEST]")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TransportFirewallApplicationTests(unittest.TestCase):
    def _instance_with_transport(self, transport_tags):
        slot = celaut.Service.Api.Slot(
            port=80,
            transport=celaut.Service.Api.Protocol(tags=transport_tags),
        )
        uri_slot = celaut.Instance.Uri_Slot(
            internal_port=80,
            uri=[celaut.Instance.Uri(ip="1.1.1.1", port=80)],
        )
        return celaut.Instance(
            api=celaut.Service.Api(slot=[slot]),
            uri_slot=[uri_slot],
        )

    def test_ch_firewall_applies_supported_transport_in_mixed_tags(self):
        instance = self._instance_with_transport(["tcp", "sctp"])
        with patch.object(ch_firewall, "allow_connection", return_value=True) as allow_connection_mock:
            result = ch_firewall.allow_connection_to_instance(
                vmachine_id="vm-1",
                instance=instance,
                source_ip="192.168.200.10",
            )
        self.assertTrue(result)
        allow_connection_mock.assert_called_once_with(
            vmachine_id="vm-1",
            ip="1.1.1.1",
            port=80,
            protocol=vm_firewall.TransportProtocol.TCP,
            source_ip="192.168.200.10",
        )


if __name__ == "__main__":
    unittest.main()
