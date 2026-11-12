"""Attach devices to a guest and give them back (#536, phase P4)."""
import os
import unittest
from unittest.mock import MagicMock, patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.devices import admission, attach, host, spec  # noqa: E402
from src.utils.hashing import SHA256_ID  # noqa: E402

K = "a" * 64
TAG = spec.make_tag("1234", "11e8", SHA256_ID, K)


def _entry(address, kernel=K):
    return spec.NodeDevice(address=address, chip="pci:1234:11e8", kernel_hash=kernel,
                           tags=frozenset({TAG}))


def _service(devices=1):
    s = celaut.Service()
    s.container.architecture.tags.append("linux/amd64")
    s.container.resources.at_init.mem_limit = 1 << 30
    s.container.resources.at_most.mem_limit = 1 << 30
    for _ in range(devices):
        s.container.devices.add().architecture.tags.append(TAG)
    return s


class AcquireTests(unittest.TestCase):
    def test_a_service_without_devices_gets_none(self):
        self.assertEqual(attach.acquire("vm", _service(0)), [])

    def test_disabled_refuses(self):
        with patch.object(attach.inventory, "enabled", return_value=False):
            with self.assertRaises(admission.UnsupportedDevicesException) as ctx:
                attach.acquire("vm", _service())
        self.assertEqual(str(ctx.exception), admission.NOT_ATTACHING_REASON)

    def test_free_devices_are_reserved_for_the_instance(self):
        with patch.object(attach.inventory, "enabled", return_value=True), \
                patch.object(attach.inventory, "free_devices", return_value=[_entry("a"), _entry("b")]), \
                patch.object(attach.assignments, "release") as release, \
                patch.object(attach.assignments, "reserve", return_value=True) as reserve:
            got = attach.acquire("vm", _service(2))
        self.assertEqual(sorted(e.address for e in got), ["a", "b"])
        reserve.assert_called_once()
        release.assert_called_once_with("vm")

    def test_a_lost_race_reads_the_free_devices_again(self):
        free = [[_entry("a"), _entry("b")], [_entry("b")]]
        with patch.object(attach.inventory, "enabled", return_value=True), \
                patch.object(attach.inventory, "free_devices", side_effect=free), \
                patch.object(attach.assignments, "release"), \
                patch.object(attach.assignments, "reserve", side_effect=[False, True]):
            got = attach.acquire("vm", _service(1))
        self.assertEqual([e.address for e in got], ["b"])

    def test_none_free_refuses_with_the_reason(self):
        with patch.object(attach.inventory, "enabled", return_value=True), \
                patch.object(attach.inventory, "free_devices", return_value=[]), \
                patch.object(attach.assignments, "release"):
            with self.assertRaises(admission.UnsupportedDevicesException) as ctx:
                attach.acquire("vm", _service())
        self.assertIn("no free device", str(ctx.exception))

    def test_admission_checks_the_free_devices(self):
        with patch("src.devices.inventory.enabled", return_value=True), \
                patch("src.devices.inventory.free_devices", return_value=[]):
            self.assertIn("no free device", admission.refusal(_service(), "ch"))
        with patch("src.devices.inventory.enabled", return_value=True), \
                patch("src.devices.inventory.free_devices", return_value=[_entry("a")]):
            self.assertIsNone(admission.refusal(_service(), "ch"))


class HypervisorArgsTests(unittest.TestCase):
    def test_one_device_option_with_one_value_per_device(self):
        self.assertEqual(attach.ch_args([]), [])
        self.assertEqual(
            attach.ch_args([_entry("0000:00:04.0"), _entry("0000:00:05.0")]),
            ["--device", "path=/sys/bus/pci/devices/0000:00:04.0/",
             "path=/sys/bus/pci/devices/0000:00:05.0/"],
        )

    def test_the_guest_boots_the_kernel_of_the_devices(self):
        with patch("src.virtualizers.microvm.kernel_store.resolve", return_value="/store/k") as resolve:
            self.assertEqual(attach.kernel_path([_entry("a", kernel="b" * 64)]), "/store/k")
        resolve.assert_called_once_with("b" * 64)
        self.assertIsNone(attach.kernel_path([]))


class ReleaseTests(unittest.TestCase):
    def _release(self, *, reset=None, healthy=True):
        dev = host.PciDevice(address="a", driver="vfio-pci" if healthy else "nvidia", exists=True)
        with patch.object(attach.assignments, "of_instance", return_value=[{"address": "a"}]), \
                patch.object(attach.assignments, "release", return_value=["a"]) as release, \
                patch.object(attach, "_reset", return_value=reset), \
                patch.object(attach.host, "read_device", return_value=dev), \
                patch.object(attach.host, "responds", return_value=True), \
                patch.object(attach.inventory, "quarantine") as quarantine:
            freed = attach.release("vm")
        return freed, release, quarantine

    def test_a_healthy_device_is_freed(self):
        freed, release, quarantine = self._release()
        self.assertEqual(freed, ["a"])
        release.assert_called_once_with("vm")
        quarantine.assert_not_called()

    def test_a_failed_reset_quarantines_and_frees_the_row(self):
        freed, release, quarantine = self._release(reset="reset failed: EIO")
        self.assertEqual(freed, ["a"])
        quarantine.assert_called_once()
        self.assertIn("reset failed", quarantine.call_args[0][1])

    def test_a_device_that_left_vfio_is_quarantined(self):
        _, _, quarantine = self._release(healthy=False)
        self.assertIn("nvidia", quarantine.call_args[0][1])

    def test_a_live_hypervisor_keeps_its_devices(self):
        with patch.object(attach.assignments, "of_instance", return_value=[{"address": "a"}]), \
                patch.object(attach, "_wait_gone", return_value=False), \
                patch.object(attach.assignments, "release") as release:
            self.assertEqual(attach.release("vm", pid=123), [])
        release.assert_not_called()

    def test_wait_gone_for_a_missing_process(self):
        self.assertTrue(attach._wait_gone(2 ** 22 + 12345, timeout_s=0.1))

    def test_reconcile_frees_the_devices_of_a_dead_instance(self):
        with patch.object(attach.assignments, "assigned", return_value={"a": "dead", "b": "live"}), \
                patch("src.virtualizers.microvm.runtime_state.load_runtime_state",
                      side_effect=lambda i: {"pid": os.getpid()} if i == "live" else None), \
                patch.object(attach, "release") as release:
            self.assertEqual(attach.reconcile(), ["dead"])
        release.assert_called_once_with("dead")


class ResizeGuardTests(unittest.TestCase):
    def test_an_instance_with_devices_can_only_hibernate_or_wake(self):
        from src.gateway import gateway as gw

        request = celaut.ModifyServiceSystemResourcesInput(service_token="t")
        request.max_sysreq.mem_limit = 2 << 30
        request.max_sysreq.cpu_quota = 100000
        servicer = gw.Gateway.__new__(gw.Gateway)
        refund = MagicMock()

        def spend(**kwargs):
            kwargs["refund_function_container"].append(refund)
            return True

        with patch.object(gw, "parse_with_client", return_value=(request, "c")), \
                patch.object(gw, "require_father"), \
                patch.object(gw.sc, "internal_instance_exists", return_value=True), \
                patch.object(gw.activity_window, "is_open", return_value=True), \
                patch.object(gw.hibernation, "plan", return_value=gw.hibernation.RESIZE), \
                patch("src.devices.assignments.of_instance", return_value=[{"address": "a"}]), \
                patch.object(gw, "spend_mu", side_effect=spend), \
                patch.object(gw, "hotplug") as hotplug:
            with self.assertRaises(Exception) as ctx:
                list(servicer.ModifyServiceSystemResources(iter([]), MagicMock()))
        self.assertIn("only hibernate", str(ctx.exception))
        refund.assert_called_once()
        hotplug.assert_not_called()


class WakeReasonTests(unittest.TestCase):
    def test_only_a_device_refusal_reaches_the_caller(self):
        from src.manager import hibernation
        from src.virtualizers.microvm.errors import MicroVMError

        try:
            try:
                raise admission.UnsupportedDevicesException("This node has no free device.")
            except Exception as inner:
                raise MicroVMError(str(inner)) from inner
        except MicroVMError as wrapped:
            self.assertEqual(hibernation._caller_reason(wrapped), "This node has no free device.")
        self.assertEqual(hibernation._caller_reason(MicroVMError("stderr tail: /nodo/x")), "")


if __name__ == "__main__":
    unittest.main()
