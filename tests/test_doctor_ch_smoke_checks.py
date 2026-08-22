import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src.commands import doctor


class GuestSerialDeviceTests(unittest.TestCase):
    """CH gives aarch64 guests a PL011, not an 8250."""

    def test_arm_hosts_get_the_pl011(self):
        for machine in ("aarch64", "arm64"):
            with self.subTest(machine=machine):
                self.assertEqual(doctor._guest_serial_device(machine), "ttyAMA0")

    def test_x86_hosts_get_the_8250(self):
        self.assertEqual(doctor._guest_serial_device("x86_64"), "ttyS0")

    def test_the_smoke_test_asks_for_the_right_one(self):
        # Hardcoding console=ttyS0 left the serial log empty on aarch64, so the
        # "boot detected via serial output" branch was unreachable there and the
        # check degraded to "the process was still alive after 2s".
        source = Path("src/commands/doctor.py").read_text(encoding="utf-8")
        self.assertIn("console={_guest_serial_device()}", source)
        self.assertNotIn('cmdline = "root=/dev/vda rw console=ttyS0"', source)


class SmokeFailureClassificationTests(unittest.TestCase):
    def test_vcpu_failures_are_named(self):
        for stderr in (
            "Fatal error: VcpuRun(InternalError)",
            "Error: InternalError",
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(doctor._classify_ch_smoke_failure(stderr), "vcpu")

    def test_every_kernel_load_shape_is_named(self):
        # The same underlying failure reaches stderr wrapped differently depending on
        # where CH gave up. Only the first of these used to be recognised, so the rest
        # fell through to a branch that prints stderr and offers no diagnosis.
        for stderr in (
            "Fatal error: Vmm(VmCreate(KernelLoad(Pe(ReadKernelImage))))",
            "Fatal error: VmBoot(VmBoot(KernelLoad(Pe(ReadKernelImage))))",
            "Fatal error: VmBoot(VmBoot(UefiLoad(UefiTooBig)))",
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(doctor._classify_ch_smoke_failure(stderr), "kernel_load")

    def test_anything_else_stays_unknown(self):
        self.assertEqual(doctor._classify_ch_smoke_failure("permission denied"), "unknown")
        self.assertEqual(doctor._classify_ch_smoke_failure(""), "unknown")

    def test_vcpu_is_checked_before_kernel_load(self):
        # A vCPU failure is the more specific diagnosis; if both markers appear the
        # more actionable one has to win.
        stderr = "VcpuRun(InternalError) ... KernelLoad"
        self.assertEqual(doctor._classify_ch_smoke_failure(stderr), "vcpu")


class HostKernelNoteTests(unittest.TestCase):
    def _run(self, release):
        out = io.StringIO()
        with mock.patch.object(doctor.platform, "release", return_value=release):
            with redirect_stdout(out):
                doctor._doctor_host_kernel()
        return out.getvalue()

    def test_a_new_kernel_defers_to_the_smoke_test_instead_of_failing_it(self):
        # Verified on this port: CH v51.1 boots guests fine under 7.1.6 on Apple
        # Silicon, so calling that kernel a problem contradicted a passing smoke test.
        output = self._run("7.1.6-400.asahi.fc44.aarch64+16k")

        self.assertIn("smoke test below is the actual check", output)
        self.assertNotIn("[WARN]", output)

    def test_it_does_not_prescribe_an_lts_kernel_that_may_not_exist(self):
        # Asahi's kernel *is* the platform; there is no LTS to downgrade to. The LTS
        # option may still be mentioned, but only as conditional on the platform
        # having one.
        output = self._run("7.1.6-400.asahi.fc44.aarch64+16k")

        self.assertIn("where your platform", output.lower())
        self.assertNotIn("use a stable kernel", output)

    def test_supported_kernels_say_nothing_alarming(self):
        output = self._run("6.8.0-31-generic")

        self.assertNotIn("[WARN]", output)
        self.assertNotIn("[FAIL]", output)


if __name__ == "__main__":
    unittest.main()
