"""Pure QEMU launch builders: kernel cmdline, argv, virtiofs device args, and
the process-identity helpers."""
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.qemu import execute as qemu_exec
    from src.virtualizers.qemu import process as qemu_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    qemu_exec = None  # type: ignore[assignment]
    qemu_process = None  # type: ignore[assignment]


def _argv():
    return qemu_exec.build_qemu_command(
        qemu_binary="qemu-system-aarch64",
        arch="linux/arm64",
        kernel_path="/assets/arm64/Image",
        initramfs_path="/assets/arm64/initramfs",
        rootfs_path=Path("/run/vm/rootfs.ext4"),
        vcpus=2,
        mem_mib=256,
        tap_name="tap0123456789",
        mac="02:aa:bb:cc:dd:ee",
        cmdline="root=/dev/vda rw console=ttyAMA0",
        serial_log_path=Path("/run/vm/qemu.serial.log"),
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class KernelCmdlineTests(unittest.TestCase):
    def test_arm64_uses_ttyAMA0(self):
        line = qemu_exec.build_kernel_cmdline("linux/arm64", "192.168.200.5", "255.255.255.0")
        self.assertIn("console=ttyAMA0", line)
        self.assertIn("root=/dev/vda", line)
        self.assertIn("ip=192.168.200.5::192.168.200.1:255.255.255.0:::off", line)

    def test_amd64_uses_ttyS0(self):
        line = qemu_exec.build_kernel_cmdline("linux/amd64", "192.168.200.5", "255.255.255.0")
        self.assertIn("console=ttyS0", line)
        self.assertNotIn("ttyAMA0", line)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class QemuCommandTests(unittest.TestCase):
    def test_argv_is_emulated_not_kvm(self):
        argv = _argv()
        self.assertIn("-accel", argv)
        self.assertEqual(argv[argv.index("-accel") + 1], "tcg")
        self.assertNotIn("kvm", argv)

    def test_arm64_uses_virt_machine(self):
        argv = _argv()
        self.assertEqual(argv[argv.index("-machine") + 1], "virt")

    def test_amd64_uses_q35_machine(self):
        argv = qemu_exec.build_qemu_command(
            qemu_binary="qemu-system-x86_64",
            arch="linux/amd64",
            kernel_path="/assets/amd64/bzImage",
            initramfs_path="/assets/amd64/initramfs",
            rootfs_path=Path("/run/vm/rootfs.ext4"),
            vcpus=1,
            mem_mib=256,
            tap_name="tapx",
            mac="02:00:00:00:00:01",
            cmdline="root=/dev/vda rw console=ttyS0",
            serial_log_path=Path("/run/vm/qemu.serial.log"),
        )
        self.assertEqual(argv[argv.index("-machine") + 1], "q35")

    def test_kernel_initramfs_and_virtio_disk(self):
        argv = _argv()
        self.assertEqual(argv[argv.index("-kernel") + 1], "/assets/arm64/Image")
        self.assertEqual(argv[argv.index("-initrd") + 1], "/assets/arm64/initramfs")
        self.assertEqual(
            argv[argv.index("-drive") + 1],
            "if=virtio,file=/run/vm/rootfs.ext4,format=raw",
        )

    def test_tap_netdev_and_mac(self):
        argv = _argv()
        self.assertEqual(
            argv[argv.index("-netdev") + 1],
            "tap,id=net0,ifname=tap0123456789,script=no,downscript=no",
        )
        self.assertIn("virtio-net-pci,netdev=net0,mac=02:aa:bb:cc:dd:ee", argv)

    def test_serial_captured_to_file(self):
        argv = _argv()
        self.assertEqual(argv[argv.index("-serial") + 1], "file:/run/vm/qemu.serial.log")

    def test_smp_and_memory(self):
        argv = _argv()
        self.assertEqual(argv[argv.index("-smp") + 1], "2")
        self.assertEqual(argv[argv.index("-m") + 1], "256M")

    def test_binary_is_argv0(self):
        self.assertEqual(_argv()[0], "qemu-system-aarch64")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VirtiofsArgsTests(unittest.TestCase):
    def test_no_shares_is_empty(self):
        self.assertEqual(qemu_exec.build_virtiofs_args([], 256), [])

    def test_shares_add_shared_memory_and_devices(self):
        mounts = [
            {"socket": "/tmp/nodo-ch/vfs-abcd.sock", "tag": "vfs-abcd"},
        ]
        args = qemu_exec.build_virtiofs_args(mounts, 256)
        joined = " ".join(args)
        # vhost-user-fs needs a shared memory backend.
        self.assertIn("memory-backend-memfd,id=mem,size=256M,share=on", joined)
        self.assertIn("node,memdev=mem", joined)
        self.assertIn("socket,id=vfs0,path=/tmp/nodo-ch/vfs-abcd.sock", joined)
        self.assertIn("vhost-user-fs-pci,queue-size=1024,chardev=vfs0,tag=vfs-abcd", joined)

    def test_shared_mem_flag_switches_memory_form(self):
        # With shares, -m carries the bare size (the memfd object supplies RAM);
        # without shares, -m uses the <mib>M form.
        with_shares = qemu_exec.build_qemu_command(
            qemu_binary="qemu-system-aarch64", arch="linux/arm64",
            kernel_path="/k", initramfs_path="/i", rootfs_path=Path("/r"),
            vcpus=1, mem_mib=256, tap_name="t", mac="02:00:00:00:00:01",
            cmdline="c", serial_log_path=Path("/s"), has_shared_mem=True,
        )
        self.assertEqual(with_shares[with_shares.index("-m") + 1], "256")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProcessNameTests(unittest.TestCase):
    def test_visible_name_prefix(self):
        name = qemu_process.qemu_process_name("abcdef0123456789")
        self.assertEqual(name, "nodo-qemu-abcdef01")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class QmpSocketPathTests(unittest.TestCase):
    def test_qmp_socket_path_uses_short_tmp_path_for_long_hash_ids(self):
        vmachine_id = "a" * 64
        with patch.object(qemu_exec, "CH_API_SOCKET_DIR", "/tmp/nodo-ch"):
            socket_path = qemu_exec._qmp_socket_path(vmachine_id)
        self.assertEqual(str(socket_path), "/tmp/nodo-ch/qmp-aaaaaaaaaaaaaaaa.sock")
        self.assertLess(len(str(socket_path)), 108)

    def test_qmp_socket_path_independent_of_deep_cache_dir(self):
        vmachine_id = "b" * 64
        deep_cache = "/nodo/storage/" + ("x" * 80) + "/__cache__"
        with patch.object(qemu_exec, "CACHE", deep_cache), \
                patch.object(qemu_exec, "CH_API_SOCKET_DIR", "/tmp/nodo-ch"):
            runtime_dir = qemu_exec._runtime_vm_dir(vmachine_id)
            socket_path = qemu_exec._qmp_socket_path(vmachine_id)
        self.assertGreater(len(str(runtime_dir / "qmp.sock")), 108)
        self.assertLess(len(str(socket_path)), 108)


if __name__ == "__main__":
    unittest.main()
