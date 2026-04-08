import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.commands import execute as execute_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    execute_cmd = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExecuteCommandTests(unittest.TestCase):
    def _response_with_slot(self, *, protocol_tags=None, transport_tags=None):
        response = celaut.ServiceInstance()
        slot = response.instance.api.slot.add()
        slot.port = 5000
        if transport_tags:
            slot.transport.tags.extend(transport_tags)
        if protocol_tags is not None:
            slot.protocol_stack.add().tags.extend(protocol_tags)

        uri_slot = response.instance.uri_slot.add()
        uri_slot.internal_port = 5000
        uri = uri_slot.uri.add()
        uri.ip = "127.0.0.1"
        uri.port = 18080
        return response

    def test_execute_ignores_slot_with_empty_protocol_stack(self):
        response = self._response_with_slot()
        out = io.StringIO()

        with patch.object(execute_cmd, "resolve_service_hash", return_value="svc"), patch.object(
            execute_cmd.grpc, "insecure_channel"
        ) as mock_channel, patch.object(
            execute_cmd.celaut_pb2_grpc, "GatewayStub"
        ) as mock_stub_cls, patch.object(
            execute_cmd, "client_grpc", return_value=iter([response])
        ):
            mock_stub_cls.return_value.StartService = object()
            with redirect_stdout(out):
                execute_cmd.execute("svc")

        rendered = out.getvalue()
        self.assertIn("service partition ->", rendered)
        self.assertNotIn("HTTP Service", rendered)
        mock_channel.return_value.close.assert_called_once()

    def test_execute_prints_http_endpoint_when_http_is_declared_in_transport(self):
        response = self._response_with_slot(transport_tags=["http"])
        out = io.StringIO()

        with patch.object(execute_cmd, "resolve_service_hash", return_value="svc"), patch.object(
            execute_cmd.grpc, "insecure_channel"
        ) as mock_channel, patch.object(
            execute_cmd.celaut_pb2_grpc, "GatewayStub"
        ) as mock_stub_cls, patch.object(
            execute_cmd, "client_grpc", return_value=iter([response])
        ):
            mock_stub_cls.return_value.StartService = object()
            with redirect_stdout(out):
                execute_cmd.execute("svc")

        rendered = out.getvalue()
        self.assertIn("HTTP Service (Port: 5000)", rendered)
        self.assertIn("http://127.0.0.1:18080", rendered)
        mock_channel.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
