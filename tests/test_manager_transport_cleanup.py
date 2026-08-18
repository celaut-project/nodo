import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.manager import manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    manager = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ManagerTransportCleanupTests(unittest.TestCase):
    def test_stop_instance_removes_rules_using_slot_transport(self):
        instance = celaut.Instance(
            api=celaut.Service.Api(
                slot=[
                    celaut.Service.Api.Slot(
                        port=8080,
                        transport=celaut.Service.Api.Protocol(tags=["udp"]),
                    )
                ]
            ),
            uri_slot=[
                celaut.Instance.Uri_Slot(
                    internal_port=8080,
                    uri=[celaut.Instance.Uri(ip="1.1.1.1", port=53)],
                )
            ],
        )
        serialized_instance = instance.SerializeToString()

        with patch.object(manager.sc, "internal_instance_exists", side_effect=lambda id: id in {"svc", "father"}), patch.object(
            manager, "kill", return_value=True
        ), patch.object(
            manager.sc, "get_internal_father_id", return_value="father"
        ), patch.object(
            manager.sc, "get_internal_instance", return_value=serialized_instance
        ), patch.object(
            manager.sc, "get_instance_balance", return_value=42
        ), patch.object(
            manager.sc, "purge_internal", return_value=None
        ), patch.object(
            manager, "remove_firewall_rule", return_value=True
        ) as remove_rule_mock:
            refund = manager.stop_instance("svc")

        self.assertEqual(refund, 42)
        remove_rule_mock.assert_called_once_with(
            vmachine_id="father",
            ip="1.1.1.1",
            port=53,
            protocol=manager.TransportProtocol.UDP,
        )


if __name__ == "__main__":
    unittest.main()
