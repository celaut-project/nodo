"""A node refuses the devices that it cannot give, never ignores them (#536, P1)."""
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut, pack_pb2
    from src.devices import admission, spec
    from src.utils.hashing import SHA256_ID
    from src.virtualizers import selection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

K = "a" * 64


def _tag(chip="1234:11e8"):
    v, d = chip.split(":")
    return spec.make_tag(v, d, SHA256_ID, K)


def _service(devices=1, fixed=True, arch="linux/amd64"):
    s = celaut.Service()
    s.container.architecture.tags.append(arch)
    s.container.resources.at_init.mem_limit = 1 << 30
    s.container.resources.at_most.mem_limit = (1 << 30) if fixed else (2 << 30)
    for _ in range(devices):
        s.container.devices.add().architecture.tags.append(_tag())
    return s


@unittest.skipIf(IMPORT_ERROR is not None, f"import failed: {IMPORT_ERROR}")
class RefusalTests(unittest.TestCase):
    def test_no_devices_is_no_refusal(self):
        self.assertIsNone(admission.refusal(_service(devices=0)))
        self.assertIsNone(admission.refusal(None))

    def test_invalid_devices_are_refused_with_the_reason(self):
        self.assertIn("same at_init and at_most", admission.refusal(_service(fixed=False)))

    def test_emulation_path_refuses_devices(self):
        self.assertEqual(admission.refusal(_service(), "qemu"), admission.NATIVE_ONLY_REASON)

    def test_is_an_unsupported_architecture(self):
        with self.assertRaises(selection.UnsupportedArchitectureException):
            admission.enforce_supported(_service(), "qemu")

    def test_selection_refuses_devices_on_the_qemu_path(self):
        with patch.object(selection, "host_arch_tag", return_value="linux/amd64"), \
                patch.object(selection, "get_arch_tag", return_value="linux/arm64"), \
                patch.object(selection, "emulation_ready", return_value=True), \
                patch.object(selection.notice, "enforce_supported"):
            with self.assertRaises(admission.UnsupportedDevicesException) as ctx:
                selection.select_virtualizer(_service(arch="linux/arm64"))
            self.assertIn("native architecture", str(ctx.exception))
            # Without devices the same service is emulated as before.
            self.assertEqual(selection.select_virtualizer(_service(devices=0, arch="linux/arm64")), "qemu")


@unittest.skipIf(IMPORT_ERROR is not None, f"import failed: {IMPORT_ERROR}")
class AvailabilityTests(unittest.TestCase):
    def test_a_device_with_no_tags_is_refused_with_the_reason(self):
        a = admission.request_availability(celaut.ArchitectureResources(), [celaut.Device()])
        self.assertFalse(a["can_execute"])
        self.assertIn("no architecture tags", a["reason"])


@unittest.skipIf(IMPORT_ERROR is not None, f"import failed: {IMPORT_ERROR}")
class ModifyTests(unittest.TestCase):
    def test_a_change_of_devices_is_refused_before_the_charge(self):
        from src.gateway import gateway as gw

        request = celaut.ModifyServiceSystemResourcesInput(service_token="t")
        request.devices.detach.append("x")
        servicer = gw.Gateway.__new__(gw.Gateway)
        with patch.object(gw, "parse_with_client", return_value=(request, "c")), \
                patch.object(gw, "require_father"), \
                patch.object(gw.sc, "internal_instance_exists", return_value=True), \
                patch.object(gw, "spend_mu") as spend:
            with self.assertRaises(Exception) as ctx:
                list(servicer.ModifyServiceSystemResources(iter([]), MagicMock()))
        self.assertIn("ATTACH_ON_REQUEST", str(ctx.exception))
        spend.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"import failed: {IMPORT_ERROR}")
class PackerTests(unittest.TestCase):
    def _packer(self, devices, fixed=True):
        from src.packers.zip_with_dockerfile import ZipContainerPacker

        p = ZipContainerPacker.__new__(ZipContainerPacker)
        p.json = {"devices": devices}
        p.service = pack_pb2.Service()
        r = p.service.container.resources
        r.at_init.mem_limit = 1 << 30
        r.at_most.mem_limit = (1 << 30) if fixed else (2 << 30)
        return p

    def test_devices_are_packed(self):
        p = self._packer([{
            "tags": [_tag()],
            "resources": {"at_init": {"mem_limit": 16, "benchmark": {"fp32_flops": 5}},
                          "at_most": {"mem_limit": 32}},
            "attach": ["on_request", "at_start"],
        }])
        p.parseDevices()
        d = p.service.container.devices[0]
        self.assertEqual(list(d.architecture.tags), [_tag()])
        self.assertEqual(d.resources.at_init.mem_limit, 16)
        self.assertEqual(d.resources.at_most.mem_limit, 32)
        self.assertEqual(list(d.attach), [celaut.Device.ATTACH_ON_REQUEST, celaut.Device.ATTACH_AT_START])
        # The same bytes read back as a celaut.Service.
        back = celaut.Service.FromString(p.service.SerializeToString())
        self.assertEqual(list(back.container.devices[0].architecture.tags), [_tag()])

    def test_invalid_declarations_fail_the_pack(self):
        with self.assertRaises(ValueError):
            self._packer([{"tags": []}]).parseDevices()
        with self.assertRaises(ValueError):
            self._packer([{"tags": [_tag()]}], fixed=False).parseDevices()
        with self.assertRaises(ValueError):
            self._packer([{"tags": [_tag()], "attach": ["later"]}]).parseDevices()

    def test_no_devices_key_changes_nothing(self):
        p = self._packer(None)
        p.json = {}
        p.parseDevices()
        self.assertEqual(len(p.service.container.devices), 0)


if __name__ == "__main__":
    unittest.main()
