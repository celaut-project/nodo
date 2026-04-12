import subprocess
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.ch import execute as ch_execute
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    ch_execute = None  # type: ignore[assignment]


def _service_with_entrypoint(*entrypoints: str):
    if celaut is None:
        raise RuntimeError(f"Test dependency import failed: {IMPORT_ERROR}")
    service = celaut.Service()
    service.container.init.entry_path.extend(entrypoints)
    return service


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloudHypervisorExecuteHelpersTests(unittest.TestCase):
    def test_build_ch_process_args_exposes_nodo_name_in_argv0(self):
        args = ch_execute._build_ch_process_args(
            start_command=[
                "/nodo/bin/cloud-hypervisor",
                "--api-socket",
                "/tmp/ch.sock",
            ],
            vmachine_id="f47b647a-eb0f-4518-8c8e-da40654bec4d",
        )
        self.assertEqual(args[0], "nodo-ch-f47b647a")
        self.assertEqual(args[1:], ["--api-socket", "/tmp/ch.sock"])

    def test_validate_entrypoint_strict_accepts_single_absolute_path(self):
        service = _service_with_entrypoint("/bin/server")
        self.assertEqual(ch_execute._validate_entrypoint_strict(service), "/bin/server")

    def test_validate_entrypoint_strict_rejects_missing_entrypoint(self):
        service = _service_with_entrypoint()
        with self.assertRaisesRegex(ch_execute.CHExecuteError, "empty"):
            ch_execute._validate_entrypoint_strict(service)

    def test_validate_entrypoint_strict_accepts_segmented_values(self):
        service = _service_with_entrypoint("usr", "local", "bin", "server")
        self.assertEqual(ch_execute._validate_entrypoint_strict(service), "/usr/local/bin/server")

    def test_validate_entrypoint_strict_normalizes_relative_single_value(self):
        service = _service_with_entrypoint("bin/server")
        self.assertEqual(ch_execute._validate_entrypoint_strict(service), "/bin/server")

    def test_validate_entrypoint_strict_rejects_cli_arguments(self):
        service = _service_with_entrypoint("/bin/server", "--flag")
        with self.assertRaisesRegex(ch_execute.CHExecuteError, "not CLI arguments"):
            ch_execute._validate_entrypoint_strict(service)

    def test_validate_custom_initramfs_accepts_required_entries(self):
        completed = subprocess.CompletedProcess(
            args=["lsinitramfs", "/tmp/initramfs"],
            returncode=0,
            stdout="init\nbin/busybox\netc/nodo-ch-initramfs.marker\n",
            stderr="",
        )
        with patch.object(ch_execute, "_ensure_command_available"), patch.object(
            ch_execute, "_run", return_value=completed
        ):
            ch_execute._validate_custom_initramfs("/tmp/initramfs")

    def test_validate_custom_initramfs_rejects_missing_marker(self):
        completed = subprocess.CompletedProcess(
            args=["lsinitramfs", "/tmp/initramfs"],
            returncode=0,
            stdout="init\nbin/busybox\n",
            stderr="",
        )
        with patch.object(ch_execute, "_ensure_command_available"), patch.object(
            ch_execute, "_run", return_value=completed
        ):
            with self.assertRaisesRegex(ch_execute.CHExecuteError, "Missing required custom entries"):
                ch_execute._validate_custom_initramfs("/tmp/initramfs")

    def test_resolve_guest_config_targets_is_root_config_path(self):
        service = celaut.Service()
        service.container.config_declaration.path.extend(["some", "nested", "dir"])
        self.assertEqual(ch_execute._resolve_guest_config_targets(service), ["/__config__"])

    def test_resolve_domain_allowlist_records_uses_domain_tags_and_ipv4(self):
        net_res = celaut.ConfigurationFile.NetworkResolution()
        net_res.tags.extend(["google.com", "www.google.com"])

        instance = celaut.Instance()
        uri_slot = celaut.Instance.Uri_Slot()
        uri_slot.internal_port = 1
        uri_slot.uri.extend(
            [
                celaut.Instance.Uri(ip="142.250.184.14", port=443),
                celaut.Instance.Uri(ip="2001:4860:4860::8888", port=443),
            ]
        )
        instance.uri_slot.extend([uri_slot])
        net_res.peer_instances.extend([instance])

        records = ch_execute._resolve_domain_allowlist_records([net_res])
        self.assertIn(("google.com", "142.250.184.14"), records)
        self.assertIn(("www.google.com", "142.250.184.14"), records)
        self.assertNotIn(("google.com", "2001:4860:4860::8888"), records)

    def test_resolve_domain_allowlist_records_ignores_non_domain_tags(self):
        net_res = celaut.ConfigurationFile.NetworkResolution()
        net_res.tags.extend(["ERGO", "my_network", "google.com"])

        instance = celaut.Instance()
        uri_slot = celaut.Instance.Uri_Slot()
        uri_slot.internal_port = 1
        uri_slot.uri.extend([celaut.Instance.Uri(ip="8.8.8.8", port=53)])
        instance.uri_slot.extend([uri_slot])
        net_res.peer_instances.extend([instance])

        records = ch_execute._resolve_domain_allowlist_records([net_res])
        self.assertEqual(records, [("google.com", "8.8.8.8")])


if __name__ == "__main__":
    unittest.main()
