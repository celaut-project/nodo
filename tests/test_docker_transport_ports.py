import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.docker import execute as docker_execute
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    docker_execute = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DockerTransportPortBindingsTests(unittest.TestCase):
    def _service_with_slot(self, transport_tags):
        return celaut.Service(
            api=celaut.Service.Api(
                slot=[
                    celaut.Service.Api.Slot(
                        port=80,
                        transport=celaut.Service.Api.Protocol(tags=transport_tags),
                    )
                ]
            )
        )

    def test_build_port_bindings_tcp(self):
        service = self._service_with_slot(["tcp"])
        mapping = docker_execute._build_docker_port_bindings(service, {80: 30080})
        self.assertEqual(mapping, {"80/tcp": 30080})

    def test_build_port_bindings_tcp_and_udp_is_error(self):
        service = self._service_with_slot(["tcp", "udp"])
        with self.assertRaisesRegex(ValueError, "single transport"):
            docker_execute._build_docker_port_bindings(service, {80: 30080})

    def test_build_port_bindings_unsupported_transport_ignored(self):
        service = self._service_with_slot(["sctp"])
        mapping = docker_execute._build_docker_port_bindings(service, {80: 30080})
        self.assertEqual(mapping, {})

    def test_build_port_bindings_missing_transport_is_error(self):
        service = celaut.Service(api=celaut.Service.Api(slot=[celaut.Service.Api.Slot(port=80)]))
        with self.assertRaisesRegex(ValueError, "missing required transport"):
            docker_execute._build_docker_port_bindings(service, {80: 30080})


if __name__ == "__main__":
    unittest.main()
