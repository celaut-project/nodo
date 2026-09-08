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
    ModelBackend,
    RaplBackend,
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


def _rapl_tree(root: Path, packages):
    """Write fake intel-rapl package dirs. ``packages`` is {name: (uj, max_uj)}."""
    root.mkdir(parents=True, exist_ok=True)
    for name, (energy_uj, max_uj) in packages.items():
        domain = root / name
        domain.mkdir(parents=True, exist_ok=True)
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
                    "intel-rapl:0:0": (3, 100),
                    "intel-rapl:1": (5, 100),
                },
            )
            names = [p.name for p in package_domain_dirs(root)]
            self.assertEqual(names, ["intel-rapl:0", "intel-rapl:1"])

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


class AttributionTests(unittest.TestCase):
    def test_cpu_share_splits_load(self):
        inst, leftover = attribute_load(100.0, {"a": 3.0, "b": 1.0})
        self.assertAlmostEqual(inst["a"], 75.0)
        self.assertAlmostEqual(inst["b"], 25.0)
        self.assertAlmostEqual(leftover, 0.0)

    def test_zero_weight_stays_unattributed(self):
        inst, leftover = attribute_load(80.0, {"a": 0.0, "b": 0.0})
        self.assertEqual(inst, {"a": 0.0, "b": 0.0})
        self.assertAlmostEqual(leftover, 80.0)

    def test_model_idle_is_other(self):
        result = attribute(node_watts=150.0, weights={"a": 1.0}, idle_watts=30.0)
        self.assertAlmostEqual(result.instance_watts["a"], 120.0)
        self.assertAlmostEqual(result.other_watts, 30.0)
        self.assertAlmostEqual(result.instance_share["a"], 120.0 / 150.0)
        self.assertAlmostEqual(result.other_share, 30.0 / 150.0)

    def test_rapl_with_no_cpu_is_all_other(self):
        result = attribute(node_watts=40.0, weights={"a": 0.0}, idle_watts=0.0)
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

    def test_package_import_does_not_read_missing_config_as_int(self):
        # The old power.py did int(env_manager.get("MONITOR_INTERVAL")) at
        # module scope and TypeError'd. The replacement package must import
        # without touching config; energy_tick is lazy so this does not load
        # logger/ConfigManager.
        import src.manager.energy as energy

        self.assertIn("energy_tick", energy.__all__)


if __name__ == "__main__":
    unittest.main()
