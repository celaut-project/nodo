"""What every microVM boot goes through, whichever hypervisor performs it.

These used to live in ``ch/execute.py`` as twenty private helpers, and the QEMU
backend reached into them by name -- which is what made one backend a client of
the other. They are the family's, so they are tested as the family's.
"""
import gzip
import string
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.microvm import bundle, network, process, rootfs
    from src.virtualizers.microvm.errors import MicroVMError
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    bundle = network = process = rootfs = None  # type: ignore[assignment]
    MicroVMError = Exception  # type: ignore[assignment]


def _service_with_entrypoint(*entrypoints: str):
    if celaut is None:
        raise RuntimeError(f"Test dependency import failed: {IMPORT_ERROR}")
    service = celaut.Service()
    service.container.init.entry_path.extend(entrypoints)
    return service


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EntrypointValidationTests(unittest.TestCase):
    def test_a_single_absolute_path_is_taken_as_it_is(self):
        service = _service_with_entrypoint("/bin/server")
        self.assertEqual(bundle.validate_entrypoint_strict(service), "/bin/server")

    def test_no_entrypoint_at_all_is_refused_before_anything_boots(self):
        service = _service_with_entrypoint()
        with self.assertRaisesRegex(MicroVMError, "empty"):
            bundle.validate_entrypoint_strict(service)

    def test_segmented_values_are_joined(self):
        service = _service_with_entrypoint("usr", "local", "bin", "server")
        self.assertEqual(
            bundle.validate_entrypoint_strict(service), "/usr/local/bin/server"
        )

    def test_a_relative_single_value_is_normalized(self):
        service = _service_with_entrypoint("bin/server")
        self.assertEqual(bundle.validate_entrypoint_strict(service), "/bin/server")

    def test_cli_arguments_are_refused(self):
        service = _service_with_entrypoint("/bin/server", "--flag")
        with self.assertRaisesRegex(MicroVMError, "not CLI arguments"):
            bundle.validate_entrypoint_strict(service)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InitramfsValidationTests(unittest.TestCase):
    def _write_initramfs(self, tmp_path, *, marker="nodo-ch-initramfs:v1", entries=True):
        # A real gzip'd newc cpio archive, built with cpio itself, rather than a
        # mock of the listing: the validator's whole job is to read the format that
        # bash/build_ch_initramfs.sh emits and that the kernel consumes, so mocking
        # the reader would leave exactly that unverified.
        root = Path(tmp_path) / "root"
        (root / "bin").mkdir(parents=True)
        (root / "etc").mkdir(parents=True)
        if entries:
            (root / "init").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "bin" / "busybox").write_bytes(b"busybox")
        if marker is not None:
            (root / "etc" / "nodo-ch-initramfs.marker").write_text(
                f"{marker}\narch:linux/arm64\n", encoding="utf-8"
            )

        names = b"\0".join(
            str(p.relative_to(root)).encode() for p in sorted(root.rglob("*"))
        ) + b"\0"
        archive = subprocess.run(
            ["cpio", "--null", "-o", "--format=newc", "--quiet"],
            input=names,
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout

        path = Path(tmp_path) / "initramfs"
        path.write_bytes(gzip.compress(archive))
        return str(path)

    def test_required_entries_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            bundle.validate_custom_initramfs(self._write_initramfs(tmp_dir))

    def test_a_missing_marker_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            initramfs_path = self._write_initramfs(tmp_dir, marker=None)
            with self.assertRaisesRegex(MicroVMError, "Missing required custom entries"):
                bundle.validate_custom_initramfs(initramfs_path)

    def test_a_contract_version_skew_is_refused(self):
        # The initramfs is a pinned release asset and /init's half of its contract
        # with this code is not, so the pair can be bumped out of step. Without
        # this check the guest boots and parks in /init's fatal() loop instead.
        with tempfile.TemporaryDirectory() as tmp_dir:
            initramfs_path = self._write_initramfs(
                tmp_dir, marker="nodo-ch-initramfs:v99"
            )
            with self.assertRaisesRegex(MicroVMError, "contract version"):
                bundle.validate_custom_initramfs(initramfs_path)

    def test_a_non_gzip_image_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "initramfs"
            path.write_bytes(b"not gzip at all")
            with self.assertRaisesRegex(MicroVMError, "gzip"):
                bundle.validate_custom_initramfs(str(path))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestInjectionTests(unittest.TestCase):
    def test_the_config_always_lands_at_the_filesystem_root(self):
        # Deterministic whatever `config_declaration.path` says: the guest's
        # own /init reads it from there.
        service = celaut.Service()
        service.container.config_declaration.path.extend(["some", "nested", "dir"])
        self.assertEqual(rootfs.guest_config_targets(service), ["/__config__"])

    def test_runtime_disk_bytes_reports_the_image_size_the_instance_got(self):
        # The instance holds its own copy of the rootfs, so its size -- not the
        # manifest's disk_space -- is what the node committed on its behalf.
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "rootfs.ext4"
            image.write_bytes(b"\0" * 4096)
            self.assertEqual(
                rootfs.runtime_disk_bytes(log_prefix="[CH][vm-1]", rootfs_path=image),
                image.stat().st_size,
            )

    def test_runtime_disk_bytes_returns_zero_when_the_image_cannot_be_stat(self):
        # Zero means "unresolved" and sends the launcher back to the manifest; it
        # must never be persisted as the instance's disk, which would bill no disk.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.ext4"
            self.assertEqual(
                rootfs.runtime_disk_bytes(log_prefix="[CH][vm-1]", rootfs_path=missing),
                0,
            )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VMIdentityTests(unittest.TestCase):
    def test_a_vm_id_is_a_hex_hash_not_a_uuid(self):
        vmachine_id = process.generate_vmachine_id()
        self.assertEqual(len(vmachine_id), 64)
        self.assertTrue(all(c in string.hexdigits for c in vmachine_id))

    def test_the_visible_process_name_is_short_enough_to_read_in_ps(self):
        name = process.visible_process_name("nodo-ch-", "f47b647aeb0f4518")
        self.assertEqual(name, "nodo-ch-f47b647a")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestAddressingTests(unittest.TestCase):
    """The  token is the family's: same bridge, same gateway, both backends."""

    def test_an_unpinned_device_leaves_the_interface_field_empty(self):
        with patch.object(network, "GUEST_NET_DEVICE", "auto"):
            token = network.guest_ip_cmdline_token(
                vm_ip="192.168.200.5", netmask="255.255.255.0"
            )
        self.assertTrue(token.startswith("ip=192.168.200.5::"))
        self.assertTrue(token.endswith(":::off"))

    def test_a_pinned_device_is_named_in_the_token(self):
        with patch.object(network, "GUEST_NET_DEVICE", "eth0"):
            token = network.guest_ip_cmdline_token(
                vm_ip="192.168.200.5", netmask="255.255.255.0"
            )
        self.assertTrue(token.endswith(":eth0:off"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReadinessTimeoutTests(unittest.TestCase):
    """Taken as an argument: a KVM boot and the same image under TCG need
    different windows, and a typo must fail here rather than time out instantly."""

    def test_a_positive_number_is_accepted(self):
        self.assertEqual(network.ready_timeout_seconds("8"), 8.0)

    def test_a_non_number_is_refused(self):
        with self.assertRaisesRegex(MicroVMError, "Invalid GUEST_NETWORK_READY_TIMEOUT_S"):
            network.ready_timeout_seconds("soon")

    def test_zero_or_negative_is_refused(self):
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(MicroVMError, "must be > 0"):
                    network.ready_timeout_seconds(value)


if __name__ == "__main__":
    unittest.main()
