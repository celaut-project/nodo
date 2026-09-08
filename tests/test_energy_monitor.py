"""Energy cost monitoring (issue #258) — the measurement math, RAPL, and the split.

These tests import the energy package's leaf modules, not ``src.manager.maintain`` or
``sql_connection``, so they need neither ``bee_rpc`` nor a ``config.yaml``. The tables
are plain entries in ``migrate.TABLES`` and carry no logic to test.
"""

import os
import tempfile
import unittest
from pathlib import Path

from src.manager.energy.attribution import attribute, attribute_load
from src.manager.energy.backends import (
    HwmonBackend,
    IpmiBackend,
    ModelBackend,
    NvmlBackend,
    RaplBackend,
    SmartPlugBackend,
    first_reading,
    model_watts,
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
class BusyCoresTests(unittest.TestCase):
    def test_host_percentage_becomes_core_time(self):
        cores = os.cpu_count() or 1
        self.assertAlmostEqual(energy_monitor._busy_cores(100.0), float(cores))
        self.assertAlmostEqual(energy_monitor._busy_cores(50.0), cores / 2)
        self.assertAlmostEqual(energy_monitor._busy_cores(0.0), 0.0)


class UnimplementedBackendTests(unittest.TestCase):
    """A declared-but-unimplemented backend has to be safe to list."""

    def test_they_produce_no_reading_and_do_not_raise(self):
        for backend in (HwmonBackend(), IpmiBackend(), NvmlBackend(), SmartPlugBackend()):
            self.assertTrue(backend.name)
            self.assertIsNone(backend.sample(60.0))

    def test_listing_them_falls_through_to_a_backend_that_answers(self):
        model = ModelBackend(idle_watts=40, load_watts=0, cpu_percent_fn=lambda: 0)
        reading = first_reading(
            [HwmonBackend(), IpmiBackend(), NvmlBackend(), SmartPlugBackend(), model],
            5.0,
        )
        self.assertEqual(reading.backend, "model")
        self.assertAlmostEqual(reading.watts, 40)


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
