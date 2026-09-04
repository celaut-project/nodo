import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src import core_services
    from src.core_services import source_application as sa
    from src.commands import execute as execute_cmd
    from protos import celaut_pb2 as celaut
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    core_services = None  # type: ignore[assignment]
    sa = None  # type: ignore[assignment]
    execute_cmd = None  # type: ignore[assignment]
    celaut = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CoreServiceIdAccessorTests(unittest.TestCase):
    def _patch_config(self, value):
        return patch.object(core_services._env_manager, "get", return_value=value)

    def test_returns_id_for_matching_role(self):
        with self._patch_config({"source-application": "abc123"}):
            self.assertEqual(
                core_services.get_core_service_id("source-application"), "abc123"
            )

    def test_placeholder_is_treated_as_unset(self):
        with self._patch_config({"source-application": "<SET_ME>"}):
            self.assertIsNone(core_services.get_core_service_id("source-application"))

    def test_blank_id_is_unset(self):
        with self._patch_config({"source-application": "  "}):
            self.assertIsNone(core_services.get_core_service_id("source-application"))

    def test_missing_role_returns_none(self):
        with self._patch_config({"packer": "abc"}):
            self.assertIsNone(core_services.get_core_service_id("source-application"))

    def test_empty_or_invalid_config_returns_none(self):
        for value in ({}, None, "not-a-dict", [{"name": "source-application", "id": "abc"}]):
            with self._patch_config(value):
                self.assertIsNone(core_services.get_core_service_id("source-application"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ParseSourcesTests(unittest.TestCase):
    def test_json_list_of_strings(self):
        self.assertEqual(
            sa._parse_sources(b'["https://h/manifest", "https://h2/manifest"]'),
            ["https://h/manifest", "https://h2/manifest"],
        )

    def test_json_list_of_objects(self):
        payload = b'[{"manifest_url": "https://h/m"}, {"urlLink": "https://h2/m"}]'
        self.assertEqual(sa._parse_sources(payload), ["https://h/m", "https://h2/m"])

    def test_json_object_with_sources_key(self):
        self.assertEqual(
            sa._parse_sources(b'{"sources": ["https://h/m"]}'), ["https://h/m"]
        )

    def test_plaintext_fallback(self):
        self.assertEqual(
            sa._parse_sources(b"https://h/m\nhttps://h2/m\n"),
            ["https://h/m", "https://h2/m"],
        )

    def test_empty(self):
        self.assertEqual(sa._parse_sources(b"  "), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AcquireServiceTests(unittest.TestCase):
    def test_returns_false_when_not_configured(self):
        out = io.StringIO()
        with patch.object(sa, "get_core_service_id", return_value=None):
            with redirect_stdout(out):
                self.assertFalse(sa.acquire_service("svc"))
        self.assertIn("no 'source-application' core service configured", out.getvalue())

    def test_returns_false_when_no_sources(self):
        with patch.object(sa, "get_core_service_id", return_value="sa-id"), patch.object(
            sa, "lookup_sources", return_value=[]
        ):
            self.assertFalse(sa.acquire_service("svc"))

    def test_downloads_first_good_source(self):
        with patch.object(sa, "get_core_service_id", return_value="sa-id"), patch.object(
            sa, "lookup_sources", return_value=["https://h/m"]
        ), patch.object(
            sa, "download_from_manifest_url", return_value={"service_id": "svc"}
        ) as mock_dl:
            self.assertTrue(sa.acquire_service("svc"))
        mock_dl.assert_called_once_with("https://h/m")

    def test_tries_next_source_on_failure(self):
        def dl(url):
            if url == "https://bad/m":
                raise sa.PublisherError("boom")
            return {"service_id": "svc"}

        with patch.object(sa, "get_core_service_id", return_value="sa-id"), patch.object(
            sa, "lookup_sources", return_value=["https://bad/m", "https://good/m"]
        ), patch.object(sa, "download_from_manifest_url", side_effect=dl):
            self.assertTrue(sa.acquire_service("svc"))

    def test_returns_false_when_all_sources_fail(self):
        with patch.object(sa, "get_core_service_id", return_value="sa-id"), patch.object(
            sa, "lookup_sources", return_value=["https://h/m"]
        ), patch.object(
            sa, "download_from_manifest_url", return_value={"service_id": None}
        ):
            self.assertFalse(sa.acquire_service("svc"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExecuteFallbackTests(unittest.TestCase):
    def _response_with_slot(self):
        response = celaut.ServiceInstance()
        slot = response.instance.api.slot.add()
        slot.port = 5000
        slot.transport.tags.extend(["http"])
        uri_slot = response.instance.uri_slot.add()
        uri_slot.internal_port = 5000
        uri = uri_slot.uri.add()
        uri.ip = "127.0.0.1"
        uri.port = 18080
        return response

    def test_falls_back_to_acquire_then_reresolves_and_runs(self):
        response = self._response_with_slot()
        # First resolve fails, acquire succeeds, second resolve returns the hash.
        with patch.object(
            execute_cmd, "resolve_service_hash", side_effect=["", "svc"]
        ) as mock_resolve, patch.object(
            execute_cmd, "acquire_service", return_value=True
        ) as mock_acquire, patch.object(
            execute_cmd, "local_channel"
        ), patch.object(
            execute_cmd.celaut_pb2_grpc, "GatewayStub"
        ) as mock_stub_cls, patch.object(
            execute_cmd, "inspect_service"
        ), patch.object(
            execute_cmd, "client_grpc", return_value=iter([response])
        ):
            mock_stub_cls.return_value.StartService = object()
            execute_cmd.execute("svc")

        mock_acquire.assert_called_once_with("svc")
        self.assertEqual(mock_resolve.call_count, 2)

    def test_prints_not_allowed_when_acquire_fails(self):
        out = io.StringIO()
        with patch.object(
            execute_cmd, "resolve_service_hash", return_value=""
        ), patch.object(execute_cmd, "acquire_service", return_value=False):
            with redirect_stdout(out):
                execute_cmd.execute("svc")
        self.assertIn("Service not allowed.", out.getvalue())

    def test_does_not_acquire_when_already_resolvable(self):
        response = self._response_with_slot()
        with patch.object(
            execute_cmd, "resolve_service_hash", return_value="svc"
        ), patch.object(
            execute_cmd, "acquire_service"
        ) as mock_acquire, patch.object(
            execute_cmd, "local_channel"
        ), patch.object(
            execute_cmd.celaut_pb2_grpc, "GatewayStub"
        ) as mock_stub_cls, patch.object(
            execute_cmd, "inspect_service"
        ), patch.object(
            execute_cmd, "client_grpc", return_value=iter([response])
        ):
            mock_stub_cls.return_value.StartService = object()
            execute_cmd.execute("svc")

        mock_acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
