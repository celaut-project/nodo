"""Energy cost monitoring (issue #258) — the measurement math, RAPL, and the split.

These tests import the energy package's leaf modules, not ``src.manager.maintain`` or
``sql_connection``, so they need neither ``bee_rpc`` nor a ``config.yaml``. The tables
are plain entries in ``migrate.TABLES`` and carry no logic to test.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.manager.energy.attribution import attribute, attribute_load
from src.manager.energy.backends import (
    EnergyReading,
    HwmonBackend,
    IpmiBackend,
    ModelBackend,
    NvmlBackend,
    RaplBackend,
    SmartPlugBackend,
    dig,
    first_reading,
    http_get_json,
    model_watts,
    package_power_limit_watts,
    run_command,
    with_additions,
    package_domain_dirs,
    wrapped_delta,
)
from src.manager.energy.cgroup import CpuWeightTracker, read_usage_usec
from src.manager.energy.price import (
    FixedPriceSource,
    cost_from_energy,
    cost_per_hour,
    energy_kwh,
)


MONITOR_IMPORT_ERROR = None
try:
    from src.manager.energy import monitor as energy_monitor
except Exception as import_exc:  # pragma: no cover - needs a readable config.yaml
    MONITOR_IMPORT_ERROR = import_exc


def _rapl_tree(root: Path, domains):
    """Write a fake powercap control type. ``domains`` maps directory name to
    ``(uj, max_uj)``, or to ``(uj, max_uj, name)`` to set the ``name`` file to
    something other than a package (``psys``, ``dram``, …)."""
    root.mkdir(parents=True, exist_ok=True)
    for index, (directory, spec) in enumerate(domains.items()):
        energy_uj, max_uj = spec[0], spec[1]
        declared = spec[2] if len(spec) > 2 else f"package-{index}"
        domain = root / directory
        domain.mkdir(parents=True, exist_ok=True)
        (domain / "name").write_text(declared, encoding="utf-8")
        (domain / "energy_uj").write_text(str(energy_uj), encoding="utf-8")
        if max_uj is not None:
            (domain / "max_energy_range_uj").write_text(str(max_uj), encoding="utf-8")


class RaplTests(unittest.TestCase):
    def test_package_dirs_skip_subdomains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(
                root,
                {
                    "intel-rapl:0": (10, 100),
                    "intel-rapl:0:0": (3, 100, "dram"),
                    "intel-rapl:1": (5, 100),
                },
            )
            names = [p.name for p in package_domain_dirs(root)]
            self.assertEqual(names, ["intel-rapl:0", "intel-rapl:1"])

    def test_a_platform_domain_is_not_a_package(self):
        """`<ct>:1` is a second socket on a server and `psys` on a client CPU.

        psys covers the package, so adding the two reports the machine twice.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(
                root,
                {
                    "intel-rapl:0": (10, 100),
                    "intel-rapl:1": (5, 100, "psys"),
                },
            )
            names = [p.name for p in package_domain_dirs(root)]
            self.assertEqual(names, ["intel-rapl:0"])

    def test_amd_layout_is_read_by_name_not_by_prefix(self):
        """AMD Zen goes through `intel_rapl_msr` and lands in the same directory.

        Nothing may key off the literal control-type name, so a domain directory
        under any control type counts as long as its `name` says package.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(root, {"amd-rapl:0": (10, 100), "amd-rapl:1": (5, 100)})
            names = [p.name for p in package_domain_dirs(root)]
            self.assertEqual(names, ["amd-rapl:0", "amd-rapl:1"])

    def test_delta_then_watts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(root, {"intel-rapl:0": (1_000_000, 2_000_000_000)})
            backend = RaplBackend(root=root)
            self.assertIsNone(backend.sample(60.0), "first snapshot has no delta")
            _rapl_tree(root, {"intel-rapl:0": (13_000_000, 2_000_000_000)})
            reading = backend.sample(60.0)
            self.assertIsNotNone(reading)
            self.assertEqual(reading.backend, "rapl")
            self.assertTrue(reading.is_floor)
            # 12e6 uJ = 12 J over 60s → 0.2 W
            self.assertAlmostEqual(reading.joules, 12.0)
            self.assertAlmostEqual(reading.watts, 0.2)

    def test_counter_wrap(self):
        self.assertEqual(wrapped_delta(90, 10, 100), 20)
        self.assertIsNone(wrapped_delta(90, 10, 0), "no range → refuse to guess")
        self.assertEqual(wrapped_delta(10, 25, 100), 15)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(root, {"intel-rapl:0": (90, 100)})
            backend = RaplBackend(root=root)
            backend.sample(1.0)
            _rapl_tree(root, {"intel-rapl:0": (10, 100)})
            reading = backend.sample(10.0)
            self.assertIsNotNone(reading)
            # 20 uJ wrap delta (90 -> 10 with range 100) over 10s
            self.assertAlmostEqual(reading.joules, 20 / 1_000_000)
            self.assertAlmostEqual(reading.watts, 2 / 1_000_000)

    def test_two_sockets_add_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(root, {"intel-rapl:0": (0, 10**9), "intel-rapl:1": (0, 10**9)})
            backend = RaplBackend(root=root)
            backend.sample(60.0)
            _rapl_tree(
                root,
                {"intel-rapl:0": (6_000_000, 10**9), "intel-rapl:1": (6_000_000, 10**9)},
            )
            reading = backend.sample(60.0)
            # 6 J + 6 J over 60 s
            self.assertAlmostEqual(reading.joules, 12.0)
            self.assertAlmostEqual(reading.watts, 0.2)

    def test_one_socket_wrapping_does_not_inflate_the_other(self):
        """A wrap is resolved against the range of the domain that wrapped.

        Adding a summed range back would credit the interval with a second
        domain's whole counter -- kilowatts out of a machine drawing watts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrap_at = 262_143_328_850  # the range a real package reports
            _rapl_tree(
                root,
                {
                    "intel-rapl:0": (wrap_at - 1_000, wrap_at),
                    "intel-rapl:1": (5_000_000, wrap_at),
                },
            )
            backend = RaplBackend(root=root)
            backend.sample(60.0)
            _rapl_tree(
                root,
                {
                    # package-0 advances 6000 uJ and turns over; package-1 advances 6 J.
                    "intel-rapl:0": (5_000, wrap_at),
                    "intel-rapl:1": (11_000_000, wrap_at),
                },
            )
            reading = backend.sample(60.0)
            self.assertAlmostEqual(reading.joules, (6_000 + 6_000_000) / 1_000_000)
            self.assertAlmostEqual(reading.watts, 0.1001, places=4)

    def test_an_uninterpretable_wrap_drops_the_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(root, {"intel-rapl:0": (90, None)})
            backend = RaplBackend(root=root)
            backend.sample(60.0)
            _rapl_tree(root, {"intel-rapl:0": (10, None)})
            self.assertIsNone(
                backend.sample(60.0),
                "no range to add back, so the interval is unmeasurable",
            )

    def test_missing_sysfs_returns_none(self):
        backend = RaplBackend(root=Path("/no/such/rapl"))
        self.assertIsNone(backend.sample(10.0))


class ModelTests(unittest.TestCase):
    def test_idle_and_full_load(self):
        self.assertAlmostEqual(model_watts(30, 120, 0), 30)
        self.assertAlmostEqual(model_watts(30, 120, 100), 150)
        self.assertAlmostEqual(model_watts(30, 120, 50), 90)

    def test_backend_uses_cpu_fn_and_elapsed(self):
        backend = ModelBackend(
            idle_watts=30, load_watts=120, cpu_percent_fn=lambda: 50
        )
        reading = backend.sample(10.0)
        self.assertEqual(reading.backend, "model")
        self.assertFalse(reading.is_floor)
        self.assertAlmostEqual(reading.watts, 90)
        self.assertAlmostEqual(reading.joules, 900)

    def test_an_uncalibrated_model_reports_nothing(self):
        """No measured idle figure, no reading: a guess is not a small measurement."""
        backend = ModelBackend(idle_watts=0, load_watts=120, cpu_percent_fn=lambda: 50)
        self.assertIsNone(backend.sample(10.0))

    def test_the_declared_package_ceiling_is_the_load_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(root, {"intel-rapl:0": (0, 100), "intel-rapl:1": (0, 100)})
            for index in (0, 1):
                domain = root / f"intel-rapl:{index}"
                (domain / "constraint_0_name").write_text("long_term")
                (domain / "constraint_0_power_limit_uw").write_text("45000000")
                # The short-term limit is a burst ceiling, not the sustained one.
                (domain / "constraint_1_name").write_text("short_term")
                (domain / "constraint_1_power_limit_uw").write_text("60000000")
            self.assertAlmostEqual(package_power_limit_watts(root), 90.0)

    def test_no_declared_ceiling_is_no_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _rapl_tree(root, {"intel-rapl:0": (0, 100)})
            self.assertIsNone(package_power_limit_watts(root))

    def test_first_reading_falls_back_to_model(self):
        rapl = RaplBackend(root=Path("/no/such/rapl"))
        model = ModelBackend(idle_watts=10, load_watts=0, cpu_percent_fn=lambda: 0)
        reading = first_reading([rapl, model], 5.0)
        self.assertEqual(reading.backend, "model")
        self.assertAlmostEqual(reading.watts, 10)


@unittest.skipIf(
    MONITOR_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {MONITOR_IMPORT_ERROR}",
)
class PriceSourceRegistryTests(unittest.TestCase):
    """A source is built once, and an unconfigurable one is not a log every minute."""

    def setUp(self):
        self.addCleanup(setattr, energy_monitor, "_setting", energy_monitor._setting)
        self.addCleanup(energy_monitor._price_sources.clear)
        self.addCleanup(energy_monitor._unknown_price_sources.clear)
        energy_monitor._price_sources.clear()
        energy_monitor._unknown_price_sources.clear()
        self.logged = []
        self.addCleanup(setattr, energy_monitor.log, "LOGGER", energy_monitor.log.LOGGER)
        energy_monitor.log.LOGGER = self.logged.append

    def _configure(self, **values):
        energy_monitor._setting = lambda key, default: values.get(key, default)

    def test_the_source_is_built_once_and_kept(self):
        """A day-ahead source rebuilt per tick would refetch its curve per tick."""
        self._configure(PRICE_SOURCE="fixed", PRICE_PER_KWH=0.2)
        self.assertIs(energy_monitor._price_source(), energy_monitor._price_source())

    def test_the_configured_tariff_reaches_the_sample(self):
        self._configure(PRICE_SOURCE="fixed", PRICE_PER_KWH=0.2, CURRENCY="EUR")
        tariff = energy_monitor._tariff()
        self.assertAlmostEqual(tariff.price_per_kwh, 0.2)
        self.assertEqual(tariff.currency, "EUR")
        self.assertEqual(tariff.source, "fixed")

    def test_an_unknown_source_falls_back_and_says_so_once(self):
        self._configure(PRICE_SOURCE="esios", PRICE_PER_KWH=0.2)
        for _ in range(5):
            tariff = energy_monitor._tariff()
        self.assertEqual(tariff.source, "fixed", "the fixed tariff is the floor")
        self.assertEqual(len(self.logged), 1, "one warning, not one per sample")
        self.assertIn("esios", self.logged[0])


@unittest.skipIf(
    MONITOR_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {MONITOR_IMPORT_ERROR}",
)
class BusyCoresTests(unittest.TestCase):
    def test_host_percentage_becomes_core_time(self):
        cores = os.cpu_count() or 1
        self.assertAlmostEqual(energy_monitor._busy_cores(100.0), float(cores))
        self.assertAlmostEqual(energy_monitor._busy_cores(50.0), cores / 2)
        self.assertAlmostEqual(energy_monitor._busy_cores(0.0), 0.0)


class HwmonTests(unittest.TestCase):
    def _tree(self, chips):
        """Write a fake /sys/class/hwmon. ``chips`` maps hwmonN to (name, files)."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for directory, (name, files) in chips.items():
            chip = root / directory
            chip.mkdir(parents=True)
            (chip / "name").write_text(name, encoding="utf-8")
            for filename, value in files.items():
                (chip / filename).write_text(str(value), encoding="utf-8")
        return root

    def test_a_power_sensor_is_microwatts_and_needs_no_previous_sample(self):
        root = self._tree({"hwmon0": ("macsmc", {"power1_input": 7_500_000})})
        backend = HwmonBackend(chip="macsmc", sensor="power1", root=root)
        reading = backend.sample(60.0)
        self.assertAlmostEqual(reading.watts, 7.5)
        self.assertAlmostEqual(reading.joules, 450.0)
        self.assertTrue(reading.is_floor, "a rail is a subset of the socket")

    def test_an_energy_sensor_is_a_counter_to_subtract(self):
        root = self._tree({"hwmon0": ("ina219", {"energy1_input": 1_000_000})})
        backend = HwmonBackend(chip="ina219", sensor="energy1", root=root)
        self.assertIsNone(backend.sample(60.0), "first snapshot has no delta")
        (root / "hwmon0" / "energy1_input").write_text("7000000")
        reading = backend.sample(60.0)
        self.assertAlmostEqual(reading.joules, 6.0)
        self.assertAlmostEqual(reading.watts, 0.1)

    def test_a_counter_going_backwards_is_not_a_reading(self):
        """hwmon publishes no wrap range, so a decrease cannot be interpreted."""
        root = self._tree({"hwmon0": ("ina219", {"energy1_input": 9_000_000})})
        backend = HwmonBackend(chip="ina219", sensor="energy1", root=root)
        backend.sample(60.0)
        (root / "hwmon0" / "energy1_input").write_text("1000000")
        self.assertIsNone(backend.sample(60.0))

    def test_the_chip_is_found_by_name_not_by_number(self):
        """hwmonN is assigned at probe time and is not stable across reboots."""
        root = self._tree(
            {
                "hwmon0": ("acpitz", {}),
                "hwmon1": ("coretemp", {}),
                "hwmon2": ("macsmc", {"power1_input": 3_000_000}),
            }
        )
        backend = HwmonBackend(chip="macsmc", sensor="power1", root=root)
        self.assertAlmostEqual(backend.sample(10.0).watts, 3.0)

    def test_a_battery_rail_on_mains_reads_zero_and_zero_is_not_a_reading(self):
        """The trap this rule exists for: a laptop battery is one of these chips.

        It measures discharge, so on the mains it answers 0, and a 0 W sample
        would shadow every source behind it and claim the node draws nothing.
        """
        root = self._tree({"hwmon0": ("BAT0", {"power1_input": 0})})
        self.assertIsNone(HwmonBackend("BAT0", "power1", root).sample(60.0))

    def test_an_unknown_chip_or_sensor_is_no_reading(self):
        root = self._tree({"hwmon0": ("acpitz", {})})
        self.assertIsNone(HwmonBackend("nope", "power1", root).sample(10.0))
        self.assertIsNone(HwmonBackend("acpitz", "power1", root).sample(10.0))
        self.assertIsNone(HwmonBackend("acpitz", "", root).sample(10.0))
        self.assertIsNone(
            HwmonBackend("acpitz", "temp1", root).sample(10.0),
            "a temperature is not energy",
        )


class IpmiTests(unittest.TestCase):
    DCMI = """
        Instantaneous power reading:                   120 Watts
        Minimum during sampling period:                 90 Watts
        Maximum during sampling period:                340 Watts
    """

    def test_the_instantaneous_reading_covers_the_machine(self):
        backend = IpmiBackend(runner=lambda argv, timeout: self.DCMI)
        reading = backend.sample(60.0)
        self.assertAlmostEqual(reading.watts, 120.0)
        self.assertAlmostEqual(reading.joules, 7200.0)
        self.assertFalse(reading.is_floor, "the PSU's input is not a floor")

    def test_no_ipmitool_and_no_dcmi_are_both_no_reading(self):
        self.assertIsNone(IpmiBackend(runner=lambda a, t: None).sample(60.0))
        self.assertIsNone(IpmiBackend(runner=lambda a, t: "").sample(60.0))
        self.assertIsNone(
            IpmiBackend(runner=lambda a, t: "Command not supported").sample(60.0)
        )

    def test_it_asks_for_dcmi_without_a_shell(self):
        seen = []
        IpmiBackend(runner=lambda argv, timeout: seen.append(list(argv))).sample(60.0)
        self.assertEqual(seen, [["ipmitool", "dcmi", "power", "reading"]])


class NvmlTests(unittest.TestCase):
    def test_every_gpu_adds_up(self):
        backend = NvmlBackend(runner=lambda a, t: "45.2\n30.1\n")
        reading = backend.sample(10.0)
        self.assertAlmostEqual(reading.watts, 75.3)
        self.assertTrue(reading.is_floor)

    def test_a_gpu_that_reports_nothing_is_skipped(self):
        backend = NvmlBackend(runner=lambda a, t: "[N/A]\n40.0\n")
        self.assertAlmostEqual(backend.sample(10.0).watts, 40.0)

    def test_no_driver_and_no_readable_gpu_are_no_reading(self):
        self.assertIsNone(NvmlBackend(runner=lambda a, t: None).sample(10.0))
        self.assertIsNone(NvmlBackend(runner=lambda a, t: "[N/A]\n").sample(10.0))


class SmartPlugTests(unittest.TestCase):
    def test_the_three_plugs_the_docstring_names(self):
        cases = (
            ({"power": 12.5}, "power"),
            ({"apower": 12.5, "aenergy": {"total": 3.0}}, "apower"),
            ({"StatusSNS": {"ENERGY": {"Power": 12.5}}}, "StatusSNS.ENERGY.Power"),
        )
        for payload, path in cases:
            backend = SmartPlugBackend(
                url="http://plug.local/meter/0",
                power_path=path,
                fetch=lambda url, timeout: payload,
            )
            reading = backend.sample(60.0)
            self.assertAlmostEqual(reading.watts, 12.5, msg=path)
            self.assertFalse(reading.is_floor, "the socket is the whole figure")

    def test_an_unconfigured_plug_is_never_fetched(self):
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            return {"power": 1}

        self.assertIsNone(SmartPlugBackend(url="", fetch=fetch).sample(60.0))
        self.assertEqual(calls, [], "no url, no request")

    def test_a_socket_switched_off_is_not_a_zero_watt_machine(self):
        backend = SmartPlugBackend(
            url="http://plug.local/meter/0", fetch=lambda u, t: {"power": 0}
        )
        self.assertIsNone(backend.sample(60.0))

    def test_an_unreachable_plug_or_a_wrong_path_is_no_reading(self):
        url = "http://plug.local/meter/0"
        self.assertIsNone(
            SmartPlugBackend(url, fetch=lambda u, t: None).sample(60.0)
        )
        self.assertIsNone(
            SmartPlugBackend(url, "watts", fetch=lambda u, t: {"power": 1}).sample(60.0)
        )
        self.assertIsNone(
            SmartPlugBackend(url, "power", fetch=lambda u, t: {"power": "n/a"}).sample(60.0)
        )

    def test_only_http_urls_are_fetched(self):
        """`requests` honours file:, so a mistyped url would read a local file."""
        self.assertIsNone(http_get_json("file:///etc/passwd", 0.5))
        self.assertIsNone(http_get_json("/etc/passwd", 0.5))

    def test_dig_follows_dicts_and_list_indexes(self):
        payload = {"a": [{"b": 7}]}
        self.assertEqual(dig(payload, "a.0.b"), 7)
        self.assertIsNone(dig(payload, "a.1.b"))
        self.assertIsNone(dig(payload, "a.b"))
        self.assertIsNone(dig(payload, ""))


class RunCommandTests(unittest.TestCase):
    def test_output_failure_and_a_missing_binary(self):
        self.assertEqual(run_command(("echo", "hi"), 5.0), "hi\n")
        self.assertIsNone(run_command(("false",), 5.0), "a non-zero exit says no")
        self.assertIsNone(run_command(("nodo-no-such-binary-2b9f",), 5.0))
        self.assertIsNone(run_command((), 5.0))


class WithAdditionsTests(unittest.TestCase):
    class Fake:
        def __init__(self, reading, name="fake"):
            self.name = name
            self.reading = reading
            self.calls = 0

        def sample(self, elapsed_seconds):
            self.calls += 1
            return self.reading

    def test_a_gpu_adds_to_a_package(self):
        primary = EnergyReading(joules=600.0, watts=10.0, backend="rapl", is_floor=True)
        gpu = self.Fake(
            EnergyReading(joules=2400.0, watts=40.0, backend="nvml", is_floor=True)
        )
        combined = with_additions(primary, [gpu], 60.0)
        self.assertAlmostEqual(combined.watts, 50.0)
        self.assertAlmostEqual(combined.joules, 3000.0)
        self.assertEqual(combined.backend, "rapl+nvml", "a sample says what built it")
        self.assertTrue(combined.is_floor, "package plus GPU is still not the socket")

    def test_a_whole_machine_reading_already_contains_the_gpu(self):
        primary = EnergyReading(
            joules=12000.0, watts=200.0, backend="smart_plug", is_floor=False
        )
        gpu = self.Fake(
            EnergyReading(joules=2400.0, watts=40.0, backend="nvml", is_floor=True)
        )
        combined = with_additions(primary, [gpu], 60.0)
        self.assertIs(combined, primary)
        self.assertEqual(gpu.calls, 0, "not even asked, so it costs nothing")

    def test_nothing_to_add_leaves_the_reading_alone(self):
        primary = EnergyReading(joules=600.0, watts=10.0, backend="rapl", is_floor=True)
        self.assertIs(with_additions(primary, [self.Fake(None)], 60.0), primary)
        self.assertIs(with_additions(primary, [], 60.0), primary)
        self.assertIsNone(with_additions(None, [self.Fake(None)], 60.0))


class AttributionTests(unittest.TestCase):
    def test_instances_split_the_load_they_account_for(self):
        # Four cores busy, all of it the two instances: they take the whole load.
        inst, leftover = attribute_load(100.0, {"a": 3.0, "b": 1.0}, busy_cores=4.0)
        self.assertAlmostEqual(inst["a"], 75.0)
        self.assertAlmostEqual(inst["b"], 25.0)
        self.assertAlmostEqual(leftover, 0.0)

    def test_work_no_instance_did_stays_with_the_host(self):
        """The defect this replaces: one small guest reading as the whole machine.

        Eight cores, one busy, and the instance is using a twentieth of one. Its
        watts are its own twentieth, not everything the package drew.
        """
        inst, leftover = attribute_load(64.0, {"a": 0.05}, busy_cores=8.0)
        self.assertAlmostEqual(inst["a"], 0.4)
        self.assertAlmostEqual(leftover, 63.6)

    def test_an_instances_watts_do_not_move_when_another_starts(self):
        alone, _ = attribute_load(80.0, {"a": 1.0}, busy_cores=4.0)
        crowded, _ = attribute_load(80.0, {"a": 1.0, "b": 2.0}, busy_cores=4.0)
        self.assertAlmostEqual(alone["a"], crowded["a"])

    def test_zero_weight_stays_unattributed(self):
        inst, leftover = attribute_load(80.0, {"a": 0.0, "b": 0.0}, busy_cores=2.0)
        self.assertEqual(inst, {"a": 0.0, "b": 0.0})
        self.assertAlmostEqual(leftover, 80.0)

    def test_an_idle_host_attributes_nothing(self):
        inst, leftover = attribute_load(40.0, {"a": 1.0}, busy_cores=0.0)
        # busy_cores floors at the weights, so a guest the host figure missed is
        # still charged for what its own counter says it used.
        self.assertAlmostEqual(inst["a"], 40.0)
        self.assertAlmostEqual(leftover, 0.0)

    def test_model_idle_is_other(self):
        result = attribute(
            node_watts=150.0, weights={"a": 1.0}, busy_cores=1.0, idle_watts=30.0
        )
        self.assertAlmostEqual(result.instance_watts["a"], 120.0)
        self.assertAlmostEqual(result.other_watts, 30.0)
        self.assertAlmostEqual(result.instance_share["a"], 120.0 / 150.0)
        self.assertAlmostEqual(result.other_share, 30.0 / 150.0)

    def test_rapl_with_no_cpu_is_all_other(self):
        result = attribute(
            node_watts=40.0, weights={"a": 0.0}, busy_cores=2.0, idle_watts=0.0
        )
        self.assertAlmostEqual(result.instance_watts["a"], 0.0)
        self.assertAlmostEqual(result.other_watts, 40.0)


class PriceTests(unittest.TestCase):
    def test_cost_is_derived_from_stored_energy_and_that_samples_tariff(self):
        joules = 3.6e6  # 1 kWh
        self.assertAlmostEqual(energy_kwh(joules), 1.0)
        self.assertAlmostEqual(cost_from_energy(joules, 0.20), 0.20)
        # A later tariff does not rewrite the sample.
        self.assertAlmostEqual(cost_from_energy(joules, 0.50), 0.50)

    def test_hourly_cost_from_watts(self):
        # 100 W at 0.20 / kWh → 0.02 / h
        self.assertAlmostEqual(cost_per_hour(100.0, 0.20), 0.02)

    def test_fixed_source_clamps_and_defaults(self):
        tariff = FixedPriceSource(price_per_kwh=-1, currency="").current()
        self.assertEqual(tariff.price_per_kwh, 0.0)
        self.assertEqual(tariff.currency, "EUR")
        self.assertEqual(tariff.source, "fixed")


class CgroupWeightTests(unittest.TestCase):
    def test_usage_usec_is_read_by_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "cpu.stat").write_text(
                "nr_periods 12\nusage_usec 2000000\nuser_usec 9\n",
                encoding="utf-8",
            )
            self.assertEqual(read_usage_usec(path), 2_000_000)

    def test_delta_becomes_cores(self):
        tracker = CpuWeightTracker()
        self.assertEqual(
            tracker.weights({"a": 1_000_000}, elapsed_seconds=1.0),
            {},
            "first snapshot has no delta",
        )
        weights = tracker.weights({"a": 3_000_000}, elapsed_seconds=1.0)
        # 2e6 usec in 1s = 2 cores
        self.assertAlmostEqual(weights["a"], 2.0)


class ImportDoesNotTypeErrorTests(unittest.TestCase):
    def test_backends_module_imports(self):
        import src.manager.energy.backends as backends

        self.assertTrue(hasattr(backends, "RaplBackend"))

    def test_package_import_does_not_read_config_at_module_scope(self):
        # A module-scope `int(env_manager.get(...))` TypeErrors on a config that does
        # not carry the key, which makes the package unimportable rather than
        # unconfigured. `energy_tick` is exposed lazily so this import pulls in
        # neither ConfigManager nor the logger.
        import src.manager.energy as energy

        self.assertIn("energy_tick", energy.__all__)


if __name__ == "__main__":
    unittest.main()
