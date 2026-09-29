"""An arm64 host whose KVM cannot create CH's GICv3/ITS is named, not crashed into (#440)."""
import errno
import io
import struct
import unittest
from contextlib import redirect_stdout
from unittest import mock

from src.commands import doctor
from src.utils.architectures import resolve_supported_architectures
from src.virtualizers.ch import vgic

# Verbatim from the issue.
ISSUE_STDERR = (
    "cloud-hypervisor:   0.009587s: <main> ERROR:/project/cloud-hypervisor/src/lib.rs:23 -- "
    "Fatal error: VmBoot(VmBoot(DeviceManager(CreateInterruptController(CreateGic(CreateVgic("
    "Vgic error CreateGic(CreateDevice(No such device (os error 19)))))))))"
)


def _fake_kvm(supported_devices, create_vm_error=None):
    """An ioctl that answers like KVM with ops registered for ``supported_devices``."""
    calls = []

    def ioctl(fd, request, arg=0, mutate=False):
        if request == vgic.KVM_CREATE_VM:
            if create_vm_error:
                raise OSError(create_vm_error, "create vm")
            return 101
        assert request == vgic.KVM_CREATE_DEVICE
        device_type, _fd, flags = struct.unpack("III", bytes(arg))
        calls.append((device_type, flags))
        if device_type not in supported_devices:
            raise OSError(errno.ENODEV, "No such device")
        return 0

    return ioctl, calls


class ProbeTests(unittest.TestCase):
    def _probe(self, supported_devices, create_vm_error=None):
        ioctl, calls = _fake_kvm(supported_devices, create_vm_error)
        with mock.patch.object(vgic.os, "open", return_value=100), mock.patch.object(
            vgic.os, "close"
        ) as close, mock.patch.object(vgic.fcntl, "ioctl", side_effect=ioctl):
            result = vgic.probe(machine="aarch64")
        return result, calls, close

    def test_a_gicv3_host_is_ok(self):
        result, calls, close = self._probe(
            {vgic.KVM_DEV_TYPE_ARM_VGIC_V3, vgic.KVM_DEV_TYPE_ARM_VGIC_ITS}
        )
        self.assertEqual(result.status, vgic.OK)
        # Only ever asked, never created: every request carries the TEST flag.
        self.assertTrue(all(flags == vgic.KVM_CREATE_DEVICE_TEST for _, flags in calls))
        # Both the VM and /dev/kvm are closed again.
        self.assertEqual(sorted(c.args[0] for c in close.call_args_list), [100, 101])

    def test_a_gicv2_host_is_missing_and_says_so(self):
        # A Raspberry Pi 4/5: GIC-400, so KVM registers only the v2 device.
        result, _, _ = self._probe({vgic.KVM_DEV_TYPE_ARM_VGIC_V2})
        self.assertTrue(result.missing)
        self.assertIn("GICv3", result.detail)
        self.assertIn("only offers a GICv2", result.detail)

    def test_a_host_without_its_is_missing(self):
        result, _, _ = self._probe({vgic.KVM_DEV_TYPE_ARM_VGIC_V3})
        self.assertTrue(result.missing)
        self.assertIn("ITS", result.detail)

    def test_a_kvm_that_cannot_be_asked_is_unknown_not_missing(self):
        result, _, _ = self._probe(set(), create_vm_error=errno.EPERM)
        self.assertEqual(result.status, vgic.UNKNOWN)

    def test_no_dev_kvm_is_unknown(self):
        with mock.patch.object(vgic.os, "open", side_effect=FileNotFoundError(2, "nope")):
            self.assertEqual(vgic.probe(machine="aarch64").status, vgic.UNKNOWN)

    def test_x86_is_not_asked(self):
        with mock.patch.object(vgic.os, "open") as open_:
            self.assertEqual(vgic.probe(machine="x86_64").status, vgic.NOT_APPLICABLE)
        open_.assert_not_called()

    def test_ioctl_numbers_match_linux_kvm_h(self):
        # _IOWR(0xAE, 0xe0, struct kvm_create_device{u32 type, fd, flags})
        self.assertEqual(vgic.KVM_CREATE_DEVICE, (3 << 30) | (12 << 16) | (0xAE << 8) | 0xE0)
        self.assertEqual(struct.calcsize("III"), 12)


class StderrClassificationTests(unittest.TestCase):
    def test_the_issue_stderr_is_a_vgic_failure(self):
        self.assertTrue(vgic.is_vgic_failure(ISSUE_STDERR))
        self.assertEqual(doctor._classify_ch_smoke_failure(ISSUE_STDERR), "vgic")

    def test_other_failures_are_not(self):
        self.assertFalse(vgic.is_vgic_failure("Fatal error: VmBoot(VmBoot(KernelLoad(x)))"))
        self.assertFalse(vgic.is_vgic_failure(""))


class AdvertisedArchitectureTests(unittest.TestCase):
    def test_native_arch_is_dropped_when_kvm_cannot_boot_it(self):
        supported = resolve_supported_architectures(
            "linux/arm64", lambda arch: arch == "linux/amd64", lambda: False
        )
        self.assertEqual([entry[0] for entry in supported], ["linux/amd64"])

    def test_native_arch_stays_by_default(self):
        supported = resolve_supported_architectures("linux/arm64", lambda arch: False)
        self.assertEqual([entry[0] for entry in supported], ["linux/arm64"])


class DoctorVgicTests(unittest.TestCase):
    def _run(self, result):
        out = io.StringIO()
        with mock.patch.object(doctor.ch_vgic, "probe", return_value=result):
            with redirect_stdout(out):
                doctor._doctor_vgic()
        return out.getvalue()

    def test_missing_is_a_fail_with_guidance(self):
        output = self._run(vgic.VgicProbe(vgic.MISSING, "KVM cannot create a GICv3"))
        self.assertIn("[FAIL] KVM cannot create a GICv3", output)
        self.assertIn(vgic.GUIDANCE, output)

    def test_ok_is_ok(self):
        self.assertIn("[OK]", self._run(vgic.VgicProbe(vgic.OK, "fine")))

    def test_x86_prints_nothing(self):
        self.assertEqual(self._run(vgic.VgicProbe(vgic.NOT_APPLICABLE, "x86")), "")


if __name__ == "__main__":
    unittest.main()
