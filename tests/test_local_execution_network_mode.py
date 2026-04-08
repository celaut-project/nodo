import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    import src.gateway.launcher.local_execution.local_execution as local_execute
    from src.utils.utils import to_gas_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    local_execute = None  # type: ignore[assignment]
    to_gas_amount = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LocalExecutionNetworkModeTests(unittest.TestCase):
    def test_external_execute_advertises_host_ip_for_reserved_external_dev_client(self):
        config = celaut.Configuration(
            initial_gas_amount=to_gas_amount(1234),
        )
        resources = celaut.Service.Container.Resources(at_init=celaut.Sysresources(mem_limit=128))
        service = celaut.Service(
            api=celaut.Service.Api(
                slot=[
                    celaut.Service.Api.Slot(
                        port=8080,
                        transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                    )
                ]
            )
        )
        metadata = celaut.Metadata()

        config_values = {
            "network.ISOLATE_INTERNAL_CHILDREN": True,
            "network.CONSIDER_DEV_AS_INTERNAL": True,
            "network.DISABLE_EXPOSE_OUTSIDE": False,
            "network.FREE_PORTS_RANGE": [],
            "network.PUBLIC_IP": "",
            "network.EXTERNAL_INTERFACE": "",
        }

        with patch.object(
            local_execute.env_manager,
            "get",
            side_effect=lambda key, default=None: config_values.get(key, default),
        ), patch.object(
            local_execute, "get_configured_virtualizer", return_value="docker"
        ), patch.object(
            local_execute, "build", return_value="svc-hash"
        ), patch.object(
            local_execute.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            local_execute, "resolve_slot_transport_protocols", return_value="tcp"
        ), patch.object(
            local_execute, "get_free_port", return_value=51000
        ), patch.object(
            local_execute, "execute", return_value=("vm-1", "192.168.200.78")
        ), patch.object(
            local_execute, "_get_external_advertised_host_ip", return_value="203.0.113.25"
        ), patch.object(
            local_execute, "provision_vmachine"
        ):
            instance = local_execute.local_execution(
                config=config,
                resources=resources,
                father_id="dev-external-1",
                father_ip="127.0.0.1",
                metadata=metadata,
                service=service,
                service_id="svc-hash",
                refund_gas=[],
            )

        self.assertEqual(instance.instance.uri_slot[0].internal_port, 8080)
        self.assertEqual(instance.instance.uri_slot[0].uri[0].ip, "203.0.113.25")
        self.assertEqual(instance.instance.uri_slot[0].uri[0].port, 51000)


if __name__ == "__main__":
    unittest.main()
