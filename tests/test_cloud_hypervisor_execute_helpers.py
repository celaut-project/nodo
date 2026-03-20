import subprocess
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.cloud_hypervisor import execute as ch_execute
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
    def test_validate_entrypoint_strict_accepts_single_absolute_path(self):
        service = _service_with_entrypoint("/bin/server")
        self.assertEqual(ch_execute._validate_entrypoint_strict(service), "/bin/server")

    def test_validate_entrypoint_strict_rejects_missing_entrypoint(self):
        service = _service_with_entrypoint()
        with self.assertRaisesRegex(ch_execute.CHExecuteError, "exactly one entrypoint"):
            ch_execute._validate_entrypoint_strict(service)

    def test_validate_entrypoint_strict_rejects_multiple_values(self):
        service = _service_with_entrypoint("/bin/server", "--flag")
        with self.assertRaisesRegex(ch_execute.CHExecuteError, "exactly one entrypoint"):
            ch_execute._validate_entrypoint_strict(service)

    def test_validate_entrypoint_strict_rejects_relative_path(self):
        service = _service_with_entrypoint("bin/server")
        with self.assertRaisesRegex(ch_execute.CHExecuteError, "absolute path"):
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


if __name__ == "__main__":
    unittest.main()
