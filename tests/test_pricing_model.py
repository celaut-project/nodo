"""The pricing model: the MU peg, per-resource scarcity, and derived deposits.

These cover the properties the old gas model got wrong rather than the arithmetic:
that a bigger instance costs more, that resources are priced independently of each
other, that a payment and a charge live on the same scale, and that a deposit is never
quietly eaten by its own transaction fee.
"""

import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.utils.config import ConfigManager
    from src.utils.cost_functions import execution_cost
    from src.utils.monetary import MU_PER_ERG, erg_to_mu, mu_to_erg_str, prices
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ConfigManager = None  # type: ignore[assignment]


BASE_PRICES = {
    "pricing.RAM_ERG_PER_GIB_HOUR": "0.001",
    "pricing.CPU_ERG_PER_VCPU_HOUR": "0.004",
    "pricing.DISK_ERG_PER_GIB_HOUR": "0.0001",
    "pricing.NET_ERG_PER_GIB": "0.002",
    "pricing.BUILD_ERG": "0.01",
    "pricing.TUNNEL_OPEN_ERG": "0.00001",
    "pricing.MODIFY_RESOURCES_ERG": "0.00001",
    "pricing.SCARCITY_MAX_MULTIPLIER": 10,
    "pricing.SCARCITY_CURVE": 1.0,
    "free_tier.CREDIT_ERG_PER_NEW_CLIENT": "0",
    "free_tier.FREE_WHILE_SCARCITY_BELOW": 0.0,
}


def _config(**overrides):
    values = dict(BASE_PRICES)
    values.update(overrides)
    manager = ConfigManager()
    real_get = manager.get

    def get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    return patch.object(manager, "get", side_effect=get)


def _sysresources(mem_gib=0.0, vcpus=0, disk_gib=0.0) -> "celaut.Sysresources":
    resources = celaut.Sysresources()
    if mem_gib:
        resources.mem_limit = int(mem_gib * 1024 ** 3)
    if disk_gib:
        resources.disk_space = int(disk_gib * 1024 ** 3)
    if vcpus:
        # CFS quota/period, the way the hypervisor expresses vCPUs.
        resources.cpu_period = 100_000
        resources.cpu_quota = int(100_000 * vcpus)
    return resources


IDLE = {"cpu": 0.0, "mem": 0.0, "disk": 0.0}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ThePegTests(unittest.TestCase):
    def test_one_mu_is_one_nanoerg(self):
        self.assertEqual(MU_PER_ERG, 1_000_000_000)
        self.assertEqual(erg_to_mu("1"), 1_000_000_000)
        self.assertEqual(mu_to_erg_str(1_000_000_000), "1")

    def test_the_peg_is_exact_in_both_directions(self):
        for erg in ("0.000000001", "0.05", "1", "123.456789"):
            with self.subTest(erg=erg):
                self.assertEqual(mu_to_erg_str(erg_to_mu(erg)), erg.rstrip("0").rstrip(".") if "." in erg else erg)

    def test_sub_nanoerg_precision_is_refused_not_rounded(self):
        """Silently rounding a price would make the node charge something else."""
        with self.assertRaises(ValueError):
            erg_to_mu("0.0000000001")

    def test_a_charge_and_a_payment_are_on_the_same_scale(self):
        """The defect the peg exists to prevent.

        Under the old model a maintenance tick converted to 0 nanoERG and a deposit
        was 1e64 gas, so nothing a node charged could ever be settled.
        """
        with _config():
            tick_mu = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=0.25, vcpus=1, disk_gib=10),
                seconds=10,
                scarcity=IDLE,
            )
        self.assertGreater(tick_mu, 0)
        # An hour of this instance is worth a readable fraction of an ERG.
        self.assertEqual(mu_to_erg_str(tick_mu * 360), "0.00524952")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PerResourcePricingTests(unittest.TestCase):
    def test_a_bigger_instance_costs_more(self):
        """The single availability scalar priced 128 MiB and 8 GiB almost identically."""
        with _config():
            small = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=0.125), seconds=3600, scarcity=IDLE
            )
            big = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=8), seconds=3600, scarcity=IDLE
            )
        self.assertEqual(big, small * 64)

    def test_cpu_and_disk_are_billed_at_all(self):
        """They never were: the old code read `cpu_limit`/`disk_limit`, which do not
        exist on Sysresources, so both always read as absent."""
        with _config():
            cpu_only = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(vcpus=2), seconds=3600, scarcity=IDLE
            )
            disk_only = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(disk_gib=100), seconds=3600, scarcity=IDLE
            )
        self.assertEqual(cpu_only, 2 * erg_to_mu("0.004"))
        self.assertEqual(disk_only, 100 * erg_to_mu("0.0001"))

    def test_vcpus_come_from_the_cfs_quota_pair(self):
        units = execution_cost.requested_units(_sysresources(vcpus=2.5))
        self.assertEqual(float(units[execution_cost.CPU]), 2.5)

    def test_charging_twice_for_half_the_time_costs_the_same(self):
        """The price of an hour cannot depend on how often the manager ticks."""
        with _config():
            resources = _sysresources(mem_gib=4)
            once = execution_cost.maintenance_charge_mu(
                system_resources=resources, seconds=3600, scarcity=IDLE
            )
            twice = 2 * execution_cost.maintenance_charge_mu(
                system_resources=resources, seconds=1800, scarcity=IDLE
            )
        self.assertEqual(once, twice)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ScarcityTests(unittest.TestCase):
    def test_scarcity_applies_per_resource_not_globally(self):
        """A node short on memory but rich in disk charges more for memory only.

        This is the property that motivated the whole rework: the previous model
        collapsed every resource into one availability number.
        """
        tight_memory = {"cpu": 0.0, "mem": 1.0, "disk": 0.0}
        with _config():
            memory_bill = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=1), seconds=3600, scarcity=tight_memory
            )
            disk_bill = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(disk_gib=1), seconds=3600, scarcity=tight_memory
            )

        with _config():
            base_memory = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=1), seconds=3600, scarcity=IDLE
            )
            base_disk = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(disk_gib=1), seconds=3600, scarcity=IDLE
            )

        self.assertEqual(memory_bill, base_memory * 10)  # SCARCITY_MAX_MULTIPLIER
        self.assertEqual(disk_bill, base_disk)  # disk is untouched

    def test_the_surcharge_is_bounded_by_the_advertised_ceiling(self):
        with _config(**{"pricing.SCARCITY_MAX_MULTIPLIER": 3}):
            self.assertEqual(execution_cost.scarcity_bp(0.0), 10_000)
            self.assertEqual(execution_cost.scarcity_bp(1.0), 30_000)
            # And nothing beyond it, whatever the reading.
            self.assertEqual(execution_cost.scarcity_bp(5.0), 30_000)

    def test_an_unreadable_machine_is_priced_as_scarce_not_free(self):
        """Failing open would give the node away; failing closed only refuses a client."""
        import psutil

        with patch("psutil.virtual_memory", side_effect=psutil.Error("boom")):
            scarcity = execution_cost.system_scarcity(force_refresh=True)
        self.assertEqual(scarcity, {"cpu": 1.0, "mem": 1.0, "disk": 1.0})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FreeTierTests(unittest.TestCase):
    def test_a_zero_price_makes_that_resource_free(self):
        with _config(**{"pricing.RAM_ERG_PER_GIB_HOUR": "0"}):
            charge = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=8), seconds=3600, scarcity=IDLE
            )
        self.assertEqual(charge, 0)

    def test_free_while_idle_charges_nothing_below_the_threshold(self):
        with _config(**{"free_tier.FREE_WHILE_SCARCITY_BELOW": 0.5}):
            self.assertTrue(execution_cost.is_free(scarcity={"cpu": 0.1, "mem": 0.2, "disk": 0.3}))
            charge = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=8),
                seconds=3600,
                scarcity={"cpu": 0.1, "mem": 0.2, "disk": 0.3},
            )
        self.assertEqual(charge, 0)

    def test_one_busy_resource_ends_the_free_tier(self):
        """That resource is what the next client will contend for."""
        with _config(**{"free_tier.FREE_WHILE_SCARCITY_BELOW": 0.5}):
            self.assertFalse(execution_cost.is_free(scarcity={"cpu": 0.1, "mem": 0.9, "disk": 0.1}))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DerivedDepositTests(unittest.TestCase):
    def test_a_deposit_keeps_the_fee_under_the_configured_share(self):
        from src.payment_system import deposits
        from src.payment_system.contracts.ergo.interface import DEFAULT_FEE

        with _config(**{"deposits.MAX_FEE_OVERHEAD": 0.02, "deposits.REFILL_BELOW": 0.2}):
            full = deposits.full_deposit_mu()
            threshold = deposits.refill_threshold_mu()

        self.assertLessEqual(DEFAULT_FEE / full, 0.02)
        self.assertEqual(mu_to_erg_str(full), "0.05")
        self.assertEqual(threshold, full // 5)

    def test_a_deposit_is_never_below_what_the_ledger_can_settle(self):
        """Ergo refuses an output under its minimum box value, so a deposit smaller
        than min_box + fee cannot be paid at all."""
        from src.payment_system import deposits
        from src.payment_system.contracts.ergo.interface import DEFAULT_FEE, SAFE_MIN_BOX_VALUE

        with _config(**{"deposits.MAX_FEE_OVERHEAD": 1.0}):
            self.assertGreaterEqual(deposits.full_deposit_mu(), SAFE_MIN_BOX_VALUE + DEFAULT_FEE)


if __name__ == "__main__":
    unittest.main()
