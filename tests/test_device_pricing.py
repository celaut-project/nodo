"""What the devices of an instance cost, and the power they draw (#536, phase P6)."""
import unittest
from unittest.mock import MagicMock, patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.devices import pricing, spec  # noqa: E402
from src.manager import statement_check  # noqa: E402
from src.utils.hashing import SHA256_ID  # noqa: E402

K = "a" * 64
TAG = spec.make_tag("10de", "2204", SHA256_ID, K)
CHIP = "pci:10de:2204"


def _entry(address):
    return spec.NodeDevice(address=address, chip=CHIP, kernel_hash=K, tags=frozenset({TAG}))


def _service(devices=1):
    s = celaut.Service()
    s.container.resources.at_init.mem_limit = 1 << 30
    s.container.resources.at_most.mem_limit = 1 << 30
    for _ in range(devices):
        s.container.devices.add().architecture.tags.append(TAG)
    return s


class _Bound:
    def __init__(self, factor):
        self._factor = factor

    def period_index(self, elapsed):
        return 2

    def factor(self, k):
        return self._factor


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(pricing.inventory, "enabled", return_value=True),
            patch.object(pricing.inventory, "chip_price_mu_per_second", return_value=7),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_a_quote_prices_the_devices_it_would_give(self):
        with patch.object(pricing.inventory, "free_devices", return_value=[_entry("a"), _entry("b")]):
            self.assertEqual(pricing.quote_rate(_service(2)), 14)
            self.assertEqual(pricing.quote_rate(_service(0)), 0)

    def test_a_quote_with_none_free_prices_the_devices_once_free(self):
        with patch.object(pricing.inventory, "free_devices", return_value=[]), \
                patch.object(pricing.inventory, "node_devices", return_value=[_entry("a")]):
            self.assertEqual(pricing.quote_rate(_service(1)), 7)

    def test_a_tick_charges_the_held_rate_held_to_the_launch_bound(self):
        rows = [{"address": "a", "mu_per_second": 10}, {"address": "b", "mu_per_second": 10}]
        with patch.object(pricing.assignments, "of_instance", return_value=rows):
            self.assertEqual(pricing.held_rate("vm"), 20)
            self.assertEqual(pricing.tick_rate("vm", None), 20)
            # Launched at 12 MU/s with a bound of 1.21x this period: 14.
            self.assertEqual(pricing.tick_rate("vm", 12, _Bound(1.21), 100.0), 14)
            # A fall passes through whole.
            self.assertEqual(pricing.tick_rate("vm", 40, _Bound(1.21), 100.0), 20)
        self.assertEqual(pricing.charge_mu(7, 10), 70)

    def test_advertised_rates_name_each_offered_chip(self):
        with patch.object(pricing.inventory, "node_devices", return_value=[_entry("a"), _entry("b")]):
            self.assertEqual(pricing.advertised_rates(), {"device_mu_per_second:pci:10de:2204": 7})

    def test_the_movement_says_the_device_rate(self):
        row = {"mem_limit": 1, "disk_space": 2, "cpu_period": 3, "cpu_quota": 4}
        self.assertNotIn("device_mu_per_second", statement_check.tick_detail(row, 10))
        self.assertEqual(statement_check.tick_detail(row, 10, 7)["device_mu_per_second"], 7)


class TickTests(unittest.TestCase):
    def test_the_first_tick_records_the_launch_rate(self):
        from src.manager import maintain

        with patch("src.devices.pricing.held_rate", return_value=9), \
                patch.object(maintain.sc, "_execute") as execute:
            self.assertEqual(maintain._device_rate("vm", {"launch_device_mu_per_second": None}), 9)
        self.assertIn("launch_device_mu_per_second", execute.call_args[0][0])
        with patch("src.devices.pricing.tick_rate", return_value=5) as tick_rate:
            self.assertEqual(maintain._device_rate("vm", {"launch_device_mu_per_second": 9}), 5)
        self.assertEqual(tick_rate.call_args[0][1], 9)

    def test_no_devices_no_charge(self):
        from src.manager import maintain

        with patch("src.devices.pricing.held_rate", return_value=0), \
                patch.object(maintain.sc, "_execute") as execute:
            self.assertEqual(maintain._device_rate("vm", {}), 0)
        execute.assert_not_called()


class QuoteTests(unittest.TestCase):
    def test_the_maintenance_figures_include_the_devices(self):
        from src.utils.cost_functions import generate_estimated_cost as gec

        resources = _service().container.resources
        with patch.object(gec, "_enforce_devices"), \
                patch.object(gec.notice, "enforce_supported"), \
                patch.object(gec, "compute_start_service_cost", return_value=0), \
                patch.object(gec, "launch_charge_mu", return_value=100), \
                patch.object(gec, "resolve_billable_resources", side_effect=lambda r, *a, **k: r), \
                patch("src.devices.pricing.quote_rate", return_value=3):
            quote = gec.generate_estimated_cost(
                metadata=celaut.Metadata(), config=celaut.Configuration(),
                resources=resources, service=_service(), check_availability=False)
        extra = 3 * int(gec.MANAGER_ITERATION_TIME)
        self.assertEqual(int(quote.init_maintenance_cost.n), 100 + extra)
        self.assertEqual(int(quote.max_maintenance_cost.n), 100 + extra)


class EnergyTests(unittest.TestCase):
    def test_device_watts_go_to_the_node_and_the_instance(self):
        from src.manager.energy import monitor
        from src.manager.energy.attribution import attribute
        from src.manager.energy.backends import EnergyReading

        reading = EnergyReading(joules=100.0, watts=10.0, backend="model", is_floor=False)
        attributed = attribute(node_watts=10.0, weights={"vm": 1.0}, busy_cores=1.0)
        with patch("src.devices.pricing.device_watts", return_value={"vm": 350.0}):
            r, a = monitor._with_device_watts(reading, attributed, 10.0)
        self.assertEqual(r.watts, 360.0)
        self.assertEqual(r.joules, 3600.0)
        self.assertAlmostEqual(a.instance_watts["vm"], attributed.instance_watts["vm"] + 350.0)
        with patch("src.devices.pricing.device_watts", return_value={}):
            self.assertEqual(monitor._with_device_watts(reading, attributed, 10.0), (reading, attributed))

    def test_device_watts_from_the_held_chips(self):
        with patch.object(pricing.assignments, "assigned", return_value={"a": "vm", "b": "vm"}), \
                patch.object(pricing.assignments, "of_instance",
                             return_value=[{"address": "a", "chip": CHIP}, {"address": "b", "chip": CHIP}]), \
                patch.object(pricing.inventory, "chip_settings", return_value={"watts": 300}):
            self.assertEqual(pricing.device_watts(), {"vm": 600.0})


if __name__ == "__main__":
    unittest.main()
