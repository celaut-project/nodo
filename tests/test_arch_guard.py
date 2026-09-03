import unittest
from unittest.mock import patch

from src.utils import arch_guard
from src.utils.arch_guard import (
    emulation_available,
    ensure_native_arch,
    normalize_arch_tag,
)


def _binfmt(enabled_handlers):
    """Fake ``open`` over /proc/sys/fs/binfmt_misc for the named handlers.

    ``enabled_handlers`` maps handler name -> the first line the kernel would
    show ("enabled"/"disabled"). Anything not listed does not exist, which the
    kernel reports as an OSError on open.
    """
    import io

    def _fake_open(path, *args, **kwargs):
        name = path.rsplit("/", 1)[-1]
        if name not in enabled_handlers:
            raise FileNotFoundError(path)
        return io.StringIO(f"{enabled_handlers[name]}\ninterpreter /usr/bin/{name}\n")

    return _fake_open


class ArchGuardTests(unittest.TestCase):
    def test_normalize_arch_tag_known_aliases(self):
        self.assertEqual(normalize_arch_tag("x86_64"), "linux/amd64")
        self.assertEqual(normalize_arch_tag("amd64"), "linux/amd64")
        self.assertEqual(normalize_arch_tag("aarch64"), "linux/arm64")
        self.assertEqual(normalize_arch_tag("arm64"), "linux/arm64")

    @patch("platform.machine", return_value="x86_64")
    def test_ensure_native_arch_allows_matching_arch(self, _machine_mock):
        ensure_native_arch("linux/amd64", context="docker build")

    @patch("platform.machine", return_value="x86_64")
    def test_ensure_native_arch_ignores_unknown_arch(self, _machine_mock):
        ensure_native_arch("linux/riscv64", context="docker build")

    @patch("platform.machine", return_value="x86_64")
    def test_cross_arch_rejected_without_binfmt_handler(self, _machine_mock):
        with patch.object(arch_guard, "open", _binfmt({}), create=True):
            with self.assertRaisesRegex(RuntimeError, "binfmt_misc handler"):
                ensure_native_arch("linux/arm64", context="packer build")

    @patch("platform.machine", return_value="x86_64")
    def test_cross_arch_allowed_with_enabled_binfmt_handler(self, _machine_mock):
        with patch.object(
            arch_guard, "open", _binfmt({"qemu-aarch64": "enabled"}), create=True
        ):
            ensure_native_arch("linux/arm64", context="packer build")

    @patch("platform.machine", return_value="x86_64")
    def test_cross_arch_rejected_when_handler_is_registered_but_disabled(self, _machine_mock):
        with patch.object(
            arch_guard, "open", _binfmt({"qemu-aarch64": "disabled"}), create=True
        ):
            with self.assertRaisesRegex(RuntimeError, "binfmt_misc handler"):
                ensure_native_arch("linux/arm64", context="packer build")

    @patch("platform.machine", return_value="x86_64")
    def test_static_handler_name_is_accepted(self, _machine_mock):
        with patch.object(
            arch_guard, "open", _binfmt({"qemu-aarch64-static": "enabled"}), create=True
        ):
            self.assertTrue(emulation_available("arm64"))

    @patch("platform.machine", return_value="x86_64")
    def test_emulation_available_is_true_for_the_host_arch(self, _machine_mock):
        self.assertTrue(emulation_available("x86_64"))

    @patch("platform.machine", return_value="x86_64")
    def test_emulation_available_is_false_for_unknown_arch(self, _machine_mock):
        self.assertFalse(emulation_available("linux/riscv64"))

    @patch("platform.machine", return_value="x86_64")
    def test_error_message_names_the_missing_handler(self, _machine_mock):
        with patch.object(arch_guard, "open", _binfmt({}), create=True):
            with self.assertRaises(RuntimeError) as caught:
                ensure_native_arch("linux/arm64", context="packer build")
        self.assertIn("qemu-aarch64", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
