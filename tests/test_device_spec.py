"""Reading the Device messages (#536, phase P1): tags, validation, assignment."""
import unittest

from protos import celaut_pb2 as celaut
from src.devices import spec
from src.utils.hashing import SHA256_ID, SHA3_256_ID

K1 = "a" * 64
K2 = "b" * 64
ALG = SHA256_ID.hex()
ALG3 = SHA3_256_ID.hex()


def tag(chip="10de:2204", kernel=K1, alg=ALG):
    vendor, device = chip.split(":")
    return spec.make_tag(vendor, device, alg, kernel)


def device(*tags, mem_init=None, mem_most=None, bench=None, attach=()):
    d = celaut.Device()
    d.architecture.tags.extend(tags)
    if mem_init is not None:
        d.resources.at_init.mem_limit = mem_init
    if mem_most is not None:
        d.resources.at_most.mem_limit = mem_most
    for k, v in (bench or {}).items():
        d.resources.at_init.benchmark.append(celaut.Uint64KeyValue(key=k, value=v))
    d.attach.extend(attach)
    return d


def container(*devices, init=None, most=None):
    c = celaut.Service.Container()
    c.devices.extend(devices)
    if init is not None:
        for k, v in init.items():
            setattr(c.resources.at_init, k, v)
    if most is not None:
        for k, v in most.items():
            setattr(c.resources.at_most, k, v)
    return c


def entry(address, chip="10de:2204", kernel=K1, mem=24 << 30, bench=(), algs=(ALG,)):
    tags = frozenset(tag(chip, kernel, a) if a == ALG else tag(chip, kernel + "", a) for a in algs)
    return spec.NodeDevice(address=address, chip="pci:" + chip, kernel_hash=kernel,
                           tags=tags, mem_limit=mem, benchmark=tuple(bench))


class TagTests(unittest.TestCase):
    def test_round_trip(self):
        t = tag()
        self.assertEqual(t, f"pci:10de:2204+{ALG}:{K1}")
        p = spec.parse_tag(t)
        self.assertEqual((p.vendor, p.device, p.algorithm, p.hash), ("10de", "2204", ALG, K1))
        self.assertEqual(p.chip, "pci:10de:2204")
        self.assertEqual(str(p), t)

    def test_algorithm_is_an_identifier_not_a_name(self):
        self.assertIsNone(spec.parse_tag(f"pci:10de:2204+sha256:{K1}"))
        self.assertIsNotNone(spec.parse_tag(tag(alg=ALG3)))

    def test_uppercase_and_short_ids_are_not_tags(self):
        self.assertIsNone(spec.parse_tag(tag().upper()))
        self.assertIsNone(spec.parse_tag(f"pci:10de:22+{ALG}:{K1}"))
        self.assertIsNone(spec.parse_tag("nvidia/sm_89"))
        self.assertIsNone(spec.parse_tag(""))

    def test_make_tag_takes_the_algorithm_id_bytes(self):
        self.assertEqual(spec.make_tag("10DE", "2204", SHA256_ID, K1.upper()), tag())


class ValidationTests(unittest.TestCase):
    FIXED = {"mem_limit": 1 << 30, "cpu_quota": 100000, "cpu_period": 100000}

    def test_no_devices_is_valid(self):
        self.assertIsNone(spec.container_reason(container()))

    def test_empty_architecture_is_not_valid(self):
        r = spec.container_reason(container(device(), init=self.FIXED, most=self.FIXED))
        self.assertIn("no architecture tags", r)

    def test_a_tag_out_of_the_format_alone_is_not_valid(self):
        r = spec.container_reason(container(device("nvidia/sm_89"), init=self.FIXED, most=self.FIXED))
        self.assertIn("format", r)

    def test_two_chips_in_one_device_are_alternatives_and_refused(self):
        d = device(tag("10de:2204"), tag("10de:2684"))
        self.assertIn("more than one chip", spec.container_reason(container(d, init=self.FIXED, most=self.FIXED)))

    def test_one_kernel_in_two_algorithms_is_fine(self):
        d = device(tag(alg=ALG), tag(kernel="c" * 64, alg=ALG3))
        self.assertIsNone(spec.container_reason(container(d, init=self.FIXED, most=self.FIXED)))

    def test_two_kernels_with_one_algorithm_in_one_device_is_refused(self):
        d = device(tag(kernel=K1), tag(kernel=K2))
        self.assertIn("one chip and one kernel", spec.container_reason(container(d, init=self.FIXED, most=self.FIXED)))

    def test_devices_of_one_service_share_one_kernel(self):
        c = container(device(tag(kernel=K1)), device(tag("1002:744c", kernel=K2)),
                      init=self.FIXED, most=self.FIXED)
        self.assertIn("share one kernel", spec.container_reason(c))

    def test_only_on_request_is_refused(self):
        d = device(tag(), attach=[spec.ATTACH_ON_REQUEST])
        self.assertIn("ATTACH_AT_START", spec.container_reason(container(d, init=self.FIXED, most=self.FIXED)))

    def test_first_supported_attach_mode_is_used_and_unknown_values_are_skipped(self):
        self.assertEqual(spec.chosen_attach(device(tag())), spec.ATTACH_AT_START)
        d = device(tag(), attach=[spec.ATTACH_ON_REQUEST, 7, spec.ATTACH_AT_START])
        self.assertEqual(spec.chosen_attach(d), spec.ATTACH_AT_START)

    def test_device_memory_at_init_above_at_most_is_refused(self):
        d = device(tag(), mem_init=8, mem_most=4)
        self.assertIn("at_init memory", spec.container_reason(container(d, init=self.FIXED, most=self.FIXED)))

    def test_resources_must_have_at_init_equal_to_at_most(self):
        self.assertIn("at_init equal", spec.container_reason(container(device(tag()))))
        grow = dict(self.FIXED, mem_limit=2 << 30)
        self.assertIn("same at_init and at_most", spec.container_reason(
            container(device(tag()), init=self.FIXED, most=grow)))
        self.assertIsNone(spec.container_reason(container(device(tag()), init=self.FIXED, most=self.FIXED)))


class AssignmentTests(unittest.TestCase):
    def test_exclusive_one_physical_device_per_asked_device(self):
        asked = [device(tag()), device(tag())]
        found, _ = spec.assign(asked, [entry("0000:01:00.0")])
        self.assertIsNone(found)
        found, _ = spec.assign(asked, [entry("0000:01:00.0"), entry("0000:02:00.0")])
        self.assertEqual({e.address for e in found}, {"0000:01:00.0", "0000:02:00.0"})

    def test_one_card_with_two_kernels_is_one_card(self):
        asked = [device(tag(kernel=K1)), device(tag(kernel=K2))]
        free = [entry("0000:01:00.0", kernel=K1), entry("0000:01:00.0", kernel=K2)]
        found, reason = spec.assign(asked, free)
        self.assertIsNone(found)

    def test_tries_the_combinations_when_minimums_differ(self):
        big, small = 48 << 30, 24 << 30
        asked = [device(tag(), mem_init=small), device(tag(), mem_init=big)]
        free = [entry("0000:01:00.0", mem=big), entry("0000:02:00.0", mem=small)]
        found, _ = spec.assign(asked, free)
        self.assertEqual([e.address for e in found], ["0000:02:00.0", "0000:01:00.0"])

    def test_benchmark_minimum(self):
        asked = [device(tag(), bench={"fp32_flops": 100})]
        self.assertIsNone(spec.assign(asked, [entry("a", bench=(("fp32_flops", 50),))])[0])
        self.assertIsNotNone(spec.assign(asked, [entry("a", bench=(("fp32_flops", 150),))])[0])
        # Not measured: not a reason to refuse.
        self.assertIsNotNone(spec.assign(asked, [entry("a")])[0])

    def test_reason_names_the_device(self):
        _, reason = spec.assign([device(tag("1002:744c"))], [entry("a")])
        self.assertIn("Device 0", reason)

    def test_announced_misfit_counts_repeated_entries(self):
        one = entry("x").to_proto()
        asked = [device(tag()), device(tag())]
        self.assertIsNotNone(spec.announced_misfit(asked, [one]))
        self.assertIsNone(spec.announced_misfit(asked, [one, one]))
        self.assertIsNone(spec.announced_misfit([], []))
        self.assertIn("no devices", spec.announced_misfit(asked, []))


if __name__ == "__main__":
    unittest.main()
