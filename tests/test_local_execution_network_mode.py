import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    import src.gateway.launcher.local_execution.local_execution as local_execute
    from src.utils.utils import to_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    local_execute = None  # type: ignore[assignment]
    to_amount = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LocalExecutionNetworkModeTests(unittest.TestCase):
    def test_find_any_host_interface_ip_prefers_physical_interfaces_over_docker(self):
        # A second physical interface (e.g. "wlp3s0") used to sit in this fixture
        # too, expected to lose to "enp0s31f6" -- but both are tier 0 in
        # _interface_priority (only the docker/virtual tier is excluded), so which
        # one sorts first is a length/alphabetical tiebreak, not a documented
        # ethernet-over-wifi preference. Left out: it tested an unspecified
        # tiebreak, not "prefers physical over docker".
        with patch.object(
            local_execute.ni,
            "interfaces",
            return_value=["docker0", "lo", "enp0s31f6"],
        ), patch.object(
            local_execute.utils,
            "get_local_ip_from_network",
            side_effect=lambda network, allow_link_local=False: {
                "enp0s31f6": "192.168.0.6",
                "docker0": "172.17.0.1",
            }[network],
        ):
            self.assertEqual(local_execute._find_any_host_interface_ip(), "192.168.0.6")

    def test_external_execute_advertises_host_ip_for_reserved_external_dev_client(self):
        config = celaut.Configuration(
            initial_mu=to_amount(1234),
        )
        resources = celaut.Service.Container.Resources(
            at_init=celaut.Sysresources(mem_limit=128, disk_space=1024)
        )
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
            # Otherwise reaches sc.local_instance_name_exists(), which needs a
            # `local_instances` table this test's throwaway database never migrates.
            local_execute, "reserve_instance_name", return_value="daring-comet"
        ), patch.object(
            local_execute.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            local_execute, "resolve_slot_transport_protocols", return_value="tcp"
        ), patch.object(
            local_execute, "get_free_port", return_value=51000
        ), patch.object(
            # execute() returns (vmachine_id, vmachine_ip, resolved_resources): the third
            # element is what the (unexercised, since this stub never calls it)
            # register_instance callback would persist.
            local_execute, "execute",
            return_value=("vm-1", "192.168.200.78", celaut.Sysresources(mem_limit=128, disk_space=1024)),
        ), patch.object(
            # Since the execute() stub above never calls register_instance itself,
            # local_execution() falls back to registering late -- hitting a
            # `local_instances` table this test's throwaway database never migrates.
            local_execute.sc, "add_local_instance", return_value=None
        ), patch.object(
            local_execute.sc, "set_local_instance_definition", return_value=True
        ), patch.object(
            local_execute, "_get_external_advertised_host_ip", return_value="203.0.113.25"
        ):
            instance = local_execute.local_execution(
                config=config,
                resources=resources,
                father_id="dev-external-1",
                father_ip="127.0.0.1",
                metadata=metadata,
                service=service,
                service_id="svc-hash",
                refund_container=[],
            )

        self.assertEqual(instance.instance.uri_slot[0].internal_port, 8080)
        self.assertEqual(instance.instance.uri_slot[0].uri[0].ip, "203.0.113.25")
        self.assertEqual(instance.instance.uri_slot[0].uri[0].port, 51000)

    def test_local_execution_filters_reserved_instance_name_env_before_virtualizer(self):
        config = celaut.Configuration(
            initial_mu=to_amount(1234),
        )
        config.environment_variables["__nodo_instance_name"] = b"My Instance"
        config.environment_variables["APP_ENV"] = b"prod"
        resources = celaut.Service.Container.Resources(
            at_init=celaut.Sysresources(mem_limit=128, disk_space=1024)
        )
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
        captured = {}

        config_values = {
            "network.ISOLATE_INTERNAL_CHILDREN": True,
            "network.CONSIDER_DEV_AS_INTERNAL": True,
            "network.DISABLE_EXPOSE_OUTSIDE": False,
            "network.FREE_PORTS_RANGE": [],
        }

        def _fake_execute(**kwargs):
            captured["config_keys"] = sorted(kwargs["config"].environment_variables.keys())
            return ("vm-1", "192.168.200.78", celaut.Sysresources(mem_limit=128, disk_space=1024))

        with patch.object(
            local_execute.env_manager,
            "get",
            side_effect=lambda key, default=None: config_values.get(key, default),
        ), patch.object(
            local_execute, "get_configured_virtualizer", return_value="docker"
        ), patch.object(
            local_execute, "build", return_value="svc-hash"
        ), patch.object(
            local_execute, "reserve_instance_name", return_value="my-instance"
        ) as reserve_name_mock, patch.object(
            local_execute.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            local_execute, "resolve_slot_transport_protocols", return_value="tcp"
        ), patch.object(
            local_execute, "get_free_port", return_value=51000
        ), patch.object(
            local_execute, "execute", side_effect=_fake_execute
        ), patch.object(
            local_execute, "_get_external_advertised_host_ip", return_value="203.0.113.25"
        ), patch.object(
            # Same reasoning as the test above: the execute() stub never invokes
            # register_instance, so local_execution() falls back to registering
            # late, straight into a `local_instances` table this test's throwaway
            # database never migrates.
            local_execute.sc, "add_local_instance", return_value=None
        ) as add_instance_mock, patch.object(
            local_execute.sc, "set_local_instance_definition", return_value=True
        ):
            local_execute.local_execution(
                config=config,
                resources=resources,
                father_id="dev-external-1",
                father_ip="127.0.0.1",
                metadata=metadata,
                service=service,
                service_id="svc-hash",
                refund_container=[],
            )

        reserve_name_mock.assert_called_once_with(requested_name="my-instance")
        self.assertEqual(captured["config_keys"], ["APP_ENV"])
        add_instance_mock.assert_called_once()
        self.assertEqual(add_instance_mock.call_args.kwargs["name"], "my-instance")


if __name__ == "__main__":
    unittest.main()
