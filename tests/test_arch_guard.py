import unittest
from unittest.mock import patch

from src.utils.arch_guard import ensure_native_arch, normalize_arch_tag


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
    def test_ensure_native_arch_rejects_cross_arch(self, _machine_mock):
        with self.assertRaisesRegex(RuntimeError, "cross-architecture builds are disabled"):
            ensure_native_arch("linux/arm64", context="packer build")

    @patch("platform.machine", return_value="x86_64")
    def test_ensure_native_arch_ignores_unknown_arch(self, _machine_mock):
        ensure_native_arch("linux/riscv64", context="docker build")


if __name__ == "__main__":
    unittest.main()
