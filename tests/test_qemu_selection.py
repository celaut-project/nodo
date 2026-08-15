"""Backend selection: native services take CH, foreign-arch take QEMU only when
emulation is enabled and available, else the historical
UnsupportedArchitectureException."""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers import selection
    from src.virtualizers.architecture import UnsupportedArchitectureException
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    selection = None  # type: ignore[assignment]
    UnsupportedArchitectureException = Exception  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SelectVirtualizerTests(unittest.TestCase):
    def _select(self, *, service_arch, host_arch, emulation_ready):
        with patch.object(selection, "get_arch_tag", return_value=service_arch), patch.object(
            selection, "host_arch_tag", return_value=host_arch
        ), patch.object(selection, "emulation_ready", return_value=emulation_ready):
            return selection.select_virtualizer(service=object())

    def test_native_arch_selects_ch(self):
        self.assertEqual(
            self._select(service_arch="linux/amd64", host_arch="linux/amd64", emulation_ready=True),
            selection.CH,
        )

    def test_cross_arch_with_emulation_selects_qemu(self):
        # arm64 service on an x86_64 host, emulation ready -> QEMU.
        self.assertEqual(
            self._select(service_arch="linux/arm64", host_arch="linux/amd64", emulation_ready=True),
            selection.QEMU,
        )

    def test_cross_arch_without_emulation_raises(self):
        with self.assertRaises(UnsupportedArchitectureException):
            self._select(service_arch="linux/arm64", host_arch="linux/amd64", emulation_ready=False)

    def test_unknown_service_arch_falls_back_to_ch(self):
        # None arch -> defer to CH, whose own resolution raises the canonical error.
        self.assertEqual(
            self._select(service_arch=None, host_arch="linux/amd64", emulation_ready=True),
            selection.CH,
        )

    def test_undetectable_host_arch_falls_back_to_ch(self):
        self.assertEqual(
            self._select(service_arch="linux/arm64", host_arch=None, emulation_ready=True),
            selection.CH,
        )


if __name__ == "__main__":
    unittest.main()
