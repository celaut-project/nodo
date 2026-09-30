import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.utils import keyvalue
    from src.commands import execute as execute_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    execute_cmd = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExecuteCommandTests(unittest.TestCase):
    def test_generator_injects_internal_instance_name(self):
        with patch.object(execute_cmd, "get_execute_client", return_value="dev-1"):
            messages = list(
                execute_cmd.generator(
                    _hash="ab" * 32,
                    client_funding_mu=123,
                    instance_name="My Instance",
                )
            )

        config = messages[1]
        self.assertEqual(keyvalue.get(config.environment_variables, "__nodo_instance_name"), b"my-instance")

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
            execute_cmd, "local_channel"
        ) as mock_channel, patch.object(
            execute_cmd.BeeClient, "start_service", return_value=response
        ):
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
            execute_cmd, "local_channel"
        ) as mock_channel, patch.object(
            execute_cmd.BeeClient, "start_service", return_value=response
        ):
            with redirect_stdout(out):
                execute_cmd.execute("svc")

        rendered = out.getvalue()
        self.assertIn("HTTP Service (Port: 5000)", rendered)
        self.assertIn("http://127.0.0.1:18080", rendered)
        mock_channel.return_value.close.assert_called_once()

    def test_execute_prints_inspect_before_starting_service_loading(self):
        response = self._response_with_slot(transport_tags=["http"])
        events = []

        def fake_inspect(service):
            events.append(("inspect", service))

        def fake_start_service(*args, **kwargs):
            events.append(("start_service", None))
            return response

        with patch.object(execute_cmd, "resolve_service_hash", return_value="svc"), patch.object(
            execute_cmd, "inspect_service", side_effect=fake_inspect
        ), patch.object(
            execute_cmd, "local_channel"
        ) as mock_channel, patch.object(
            execute_cmd.BeeClient, "start_service", side_effect=fake_start_service
        ):
            execute_cmd.execute("svc")

        self.assertEqual(
            events[:2],
            [("inspect", "svc"), ("start_service", None)],
        )
        mock_channel.return_value.close.assert_called_once()

    def test_removed_remote_flag_is_refused_with_the_tunnel_hint(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            execute_cmd.reject_removed_remote_flag(["--remote", "svc"])

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("--remote", out.getvalue())
        self.assertIn("nodo tunnel", out.getvalue())

    def test_args_without_remote_flag_pass_through(self):
        out = io.StringIO()
        with redirect_stdout(out):
            execute_cmd.reject_removed_remote_flag(["--name", "x", "svc"])

        self.assertEqual(out.getvalue(), "")

    def test_nodo_execute_checks_for_the_removed_remote_flag(self):
        # nodo.py runs its dispatch at module level, so pin the call in its source.
        with open("nodo.py", encoding="utf-8") as f:
            self.assertIn("reject_removed_remote_flag(args)", f.read())


if __name__ == "__main__":
    unittest.main()
