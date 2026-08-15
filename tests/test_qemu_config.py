"""QEMU config resolution: emulator binary lookup, kernel/initramfs override vs
CH fallback, and the emulation-ready gate."""
import os
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.qemu import config as qemu_config
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    qemu_config = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BinaryResolutionTests(unittest.TestCase):
    def test_configured_executable_path_wins(self):
        with patch.object(qemu_config, "_binary_paths", return_value={"linux/arm64": "/opt/qemu"}), \
             patch("os.path.isfile", return_value=True), \
             patch("os.access", return_value=True):
            self.assertEqual(qemu_config.qemu_system_binary("linux/arm64"), "/opt/qemu")

    def test_configured_path_not_executable_returns_none(self):
        with patch.object(qemu_config, "_binary_paths", return_value={"linux/arm64": "/opt/qemu"}), \
             patch("os.path.isfile", return_value=False):
            self.assertIsNone(qemu_config.qemu_system_binary("linux/arm64"))

    def test_path_fallback_uses_qemu_system_name(self):
        with patch.object(qemu_config, "_binary_paths", return_value={}), \
             patch("shutil.which", return_value="/usr/bin/qemu-system-aarch64") as which:
            self.assertEqual(
                qemu_config.qemu_system_binary("linux/arm64"),
                "/usr/bin/qemu-system-aarch64",
            )
            which.assert_called_once_with("qemu-system-aarch64")

    def test_unknown_arch_returns_none(self):
        with patch.object(qemu_config, "_binary_paths", return_value={}):
            self.assertIsNone(qemu_config.qemu_system_binary("linux/riscv64"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class KernelResolutionTests(unittest.TestCase):
    def test_qemu_override_wins(self):
        with patch.object(qemu_config.env_manager, "get") as get:
            def side_effect(key, default=None):
                if key == "virtualizers.qemu.KERNEL_PATHS":
                    return {"linux/arm64": "/qemu/Image"}
                if key == "virtualizers.ch.KERNEL_PATHS":
                    return {"linux/arm64": "/ch/vmlinuz"}
                return default
            get.side_effect = side_effect
            self.assertEqual(qemu_config.qemu_kernel_path("linux/arm64"), "/qemu/Image")

    def test_falls_back_to_ch_kernel(self):
        with patch.object(qemu_config.env_manager, "get") as get:
            def side_effect(key, default=None):
                if key == "virtualizers.qemu.KERNEL_PATHS":
                    return {}
                if key == "virtualizers.ch.KERNEL_PATHS":
                    return {"linux/arm64": "/ch/vmlinuz"}
                return default
            get.side_effect = side_effect
            self.assertEqual(qemu_config.qemu_kernel_path("linux/arm64"), "/ch/vmlinuz")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EmulationReadyTests(unittest.TestCase):
    def test_requires_enabled_binary_and_assets(self):
        with patch.object(qemu_config, "qemu_enabled", return_value=True), \
             patch.object(qemu_config, "qemu_system_binary", return_value="/usr/bin/qemu-system-aarch64"), \
             patch.object(qemu_config, "guest_assets_available", return_value=True):
            self.assertTrue(qemu_config.emulation_ready("linux/arm64"))

    def test_disabled_is_not_ready(self):
        with patch.object(qemu_config, "qemu_enabled", return_value=False), \
             patch.object(qemu_config, "qemu_system_binary", return_value="/usr/bin/qemu-system-aarch64"), \
             patch.object(qemu_config, "guest_assets_available", return_value=True):
            self.assertFalse(qemu_config.emulation_ready("linux/arm64"))

    def test_missing_binary_is_not_ready(self):
        with patch.object(qemu_config, "qemu_enabled", return_value=True), \
             patch.object(qemu_config, "qemu_system_binary", return_value=None), \
             patch.object(qemu_config, "guest_assets_available", return_value=True):
            self.assertFalse(qemu_config.emulation_ready("linux/arm64"))

    def test_missing_assets_is_not_ready(self):
        with patch.object(qemu_config, "qemu_enabled", return_value=True), \
             patch.object(qemu_config, "qemu_system_binary", return_value="/usr/bin/qemu-system-aarch64"), \
             patch.object(qemu_config, "guest_assets_available", return_value=False):
            self.assertFalse(qemu_config.emulation_ready("linux/arm64"))


if __name__ == "__main__":
    unittest.main()
