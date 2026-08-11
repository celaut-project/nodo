"""The pricing model: the three units, per-resource scarcity, and derived deposits.

MU is what the node counts in, `MU_PER_NANOERG` is what an MU is worth on the ledger,
and `ui.DISPLAY_UNIT` is what the operator reads. These cover the properties the gas
model got wrong rather than the arithmetic: that a bigger instance costs more, that
resources are priced independently of each other, that a charge and a payment still
land on the same scale, and that a deposit is never eaten by its own transaction fee.

The imports below are deliberately NOT guarded by a try/skipIf. Everything here checks
arithmetic on money, and a suite that reports OK because a dependency was missing is
worse than one that fails: it claims the sums were verified when nothing ran. A missing
`mnemonic` or `psutil` must turn this file red, not green-with-skips.
"""

import unittest
from unittest.mock import patch

from protos import celaut_pb2 as celaut
# The MU rate and its conversions belong to the payment contract, not to the accounting
# core -- importing them from here is what keeps `monetary` ledger-agnostic.
from src.payment_system.contracts.ergo.rate import mu_per_erg, mu_to_nanoerg
from src.utils.config import ConfigManager
from src.utils.config_validation import validate_pricing_config
from src.utils.cost_functions import execution_cost
from src.utils.monetary import format_mu, parse_to_mu, prices


BASE_PRICES = {
    "pricing.RAM_MU_PER_GIB_HOUR": 1_000_000,
    "pricing.CPU_MU_PER_VCPU_HOUR": 4_000_000,
    "pricing.DISK_MU_PER_GIB_HOUR": 100_000,
    "pricing.NET_MU_PER_GIB": 2_000_000,
    "pricing.BUILD_MU": 10_000_000,
    "pricing.TUNNEL_OPEN_MU": 10_000,
    "pricing.MODIFY_RESOURCES_MU": 10_000,
    "pricing.SCARCITY_MAX_MULTIPLIER": 10,
    "pricing.SCARCITY_CURVE": 1.0,
    "free_tier.CREDIT_MU_PER_NEW_CLIENT": 0,
    "free_tier.FREE_WHILE_SCARCITY_BELOW": 0.0,
    "ledgers.ergo.payments.MU_PER_NANOERG": 1,
    "ui.DISPLAY_UNIT": "erg",
}


def _config(**overrides):
    values = dict(BASE_PRICES)
    values.update(overrides)
    manager = ConfigManager()
    real_get = manager.get

    def get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    return patch.object(manager, "get", side_effect=get)


def _nested_config(**overrides) -> dict:
    """BASE_PRICES as the nested mapping `validate_pricing_config` reads.

    That check runs inside `load_config`, before the ConfigManager singleton is usable,
    so it takes the parsed YAML directly rather than going through `get()`. Derived from
    BASE_PRICES rather than written out twice, so the two cannot drift.
    """
    values = dict(BASE_PRICES)
    values.update(overrides)
    config: dict = {}
    for dotted, value in values.items():
        *parents, leaf = dotted.split(".")
        node = config
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    return config


def _settlement_warnings(**overrides) -> list:
    """Non-fatal findings from the load-time pricing check."""
    warnings: list = []
    validate_pricing_config(_nested_config(**overrides), warn=warnings.append)
    return warnings


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


class TheThreeUnitsTests(unittest.TestCase):
    """MU, what it is worth, and what the operator reads are three separate things."""

    def test_one_mu_is_one_nanoerg_by_default(self):
        with _config():
            self.assertEqual(mu_per_erg(), 1_000_000_000)
            self.assertEqual(mu_to_nanoerg(1_000), 1_000)

    def test_the_ledger_rate_rescales_what_an_mu_is_worth(self):
        """The rate belongs to the payment contract, not to MU."""
        with _config(**{"ledgers.ergo.payments.MU_PER_NANOERG": "0.001"}):
            # A coarser MU: one MU is now a thousand nanoERG.
            self.assertEqual(mu_per_erg(), 1_000_000)
            self.assertEqual(mu_to_nanoerg(1_000), 1_000_000)

    def test_a_rate_that_does_not_divide_an_erg_is_refused(self):
        with _config(**{"ledgers.ergo.payments.MU_PER_NANOERG": "0.0000000003"}):
            with self.assertRaises(ValueError):
                mu_per_erg()

    def test_prices_are_whole_mu(self):
        """MU is the unit of account; there is nothing smaller to express."""
        with _config(**{"pricing.RAM_MU_PER_GIB_HOUR": "0.5"}):
            with self.assertRaises(ValueError):
                prices()

    def test_the_display_unit_is_erg_by_default(self):
        with _config():
            self.assertEqual(format_mu(1_000_000_000), "1 ERG")
            self.assertEqual(parse_to_mu("1"), 1_000_000_000)

    def test_the_operator_can_read_and_type_raw_mu(self):
        with _config(**{"ui.DISPLAY_UNIT": "mu"}):
            self.assertEqual(format_mu(14_582), "14582 MU")
            self.assertEqual(parse_to_mu("14582"), 14_582)

    def test_a_custom_unit_can_be_declared(self):
        """The hook for showing a fiat figure later. Static rate, never refreshed."""
        with _config(**{
            "ui.DISPLAY_UNIT": "usd",
            "ui.UNITS.usd": {"MU_PER_UNIT": 500_000_000, "SYMBOL": "USD", "DECIMALS": 4},
        }):
            self.assertEqual(format_mu(5_000_000_000), "10 USD")
            self.assertEqual(parse_to_mu("2.5"), 1_250_000_000)

    def test_an_undeclared_display_unit_is_refused(self):
        with _config(**{"ui.DISPLAY_UNIT": "doblones"}):
            with self.assertRaises(ValueError):
                format_mu(1)

    def test_the_accounting_core_names_no_ledger(self):
        """`monetary` must not know what an MU is worth in real money.

        The rate is a property of the payment contract, so `mu_per_nanoerg`,
        `mu_to_nanoerg`, `nanoerg_to_mu` and `mu_per_erg` live with the contract. They
        used to sit in `monetary`, which made the accounting core read one ledger's
        config section and hardcode its display unit — so a second payment system meant
        editing the core.
        """
        from src.utils import monetary

        for moved in ("mu_per_nanoerg", "mu_to_nanoerg", "nanoerg_to_mu", "mu_per_erg"):
            self.assertFalse(
                hasattr(monetary, moved),
                f"monetary.{moved} is back; the ledger rate belongs to the contract.",
            )

    def test_the_display_unit_comes_from_the_payment_contract(self):
        """ERG is on offer because the Ergo contract contributes it, not because
        `monetary` has a branch for it."""
        from src.utils import monetary

        with _config():
            self.assertIn("erg", monetary.contract_display_units())
            self.assertEqual(monetary.default_display_unit_name(), "erg")

    def test_a_node_with_no_payment_contract_falls_back_to_raw_mu(self):
        """The honest answer when nothing can say what an MU is worth.

        Display must not be the thing that breaks: `format_mu` runs on log lines, so a
        payment stack that will not import renders balances in MU rather than raising.
        """
        from src.utils import monetary

        with _config(**{"ui.DISPLAY_UNIT": None}), \
             patch.object(monetary, "contract_display_units", return_value={}):
            self.assertEqual(monetary.default_display_unit_name(), "mu")
            self.assertEqual(format_mu(14_582), "14582 MU")

    def test_a_contract_unit_cannot_be_shadowed_by_a_hand_written_one(self):
        """A ledger-derived rate is live; a hand-declared one goes stale. The live one wins."""
        with _config(**{
            "ui.DISPLAY_UNIT": "erg",
            "ui.UNITS.erg": {"MU_PER_UNIT": 1, "SYMBOL": "FAKE", "DECIMALS": 0},
        }):
            self.assertEqual(format_mu(1_000_000_000), "1 ERG")

    def test_an_amount_that_is_not_whole_mu_is_refused_not_rounded(self):
        """The operator asked for an amount and must not silently be charged another."""
        with _config():
            with self.assertRaises(ValueError):
                parse_to_mu("0.0000000001")

    def test_display_never_changes_what_is_charged(self):
        resources = _sysresources(mem_gib=1)
        with _config():
            in_erg = execution_cost.maintenance_charge_mu(
                system_resources=resources, seconds=3600, scarcity=IDLE
            )
        with _config(**{"ui.DISPLAY_UNIT": "mu"}):
            in_mu = execution_cost.maintenance_charge_mu(
                system_resources=resources, seconds=3600, scarcity=IDLE
            )
        self.assertEqual(in_erg, in_mu)

    def test_a_charge_and_a_payment_stay_on_the_same_scale(self):
        """The defect this model exists to prevent.

        Under gas a maintenance tick converted to 0 nanoERG and a deposit was 1e64 gas,
        so nothing a node charged could ever be settled.
        """
        with _config():
            tick_mu = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=0.25, vcpus=1, disk_gib=10),
                seconds=10,
                scarcity=IDLE,
            )
            self.assertGreater(tick_mu, 0)
            self.assertGreater(mu_to_nanoerg(tick_mu), 0)
            self.assertEqual(format_mu(tick_mu * 360), "0.00524952 ERG")
        # And the load-time check agrees, since that is the one that actually runs.
        self.assertEqual(_settlement_warnings(), [])

    def test_prices_dwarfed_by_the_rate_are_detected(self):
        """Exactly the gas failure: charges and payments on scales that never meet.

        Checked through `validate_pricing_config`, which is what `load_config` calls.
        A second copy of this rule used to live in `monetary` and was reached only from
        here, so the test passed while nothing verified the running node.
        """
        warnings = _settlement_warnings(**{
            "pricing.RAM_MU_PER_GIB_HOUR": 100,
            "ledgers.ergo.payments.MU_PER_NANOERG": 1_000_000,
        })
        self.assertEqual(len(warnings), 1)
        self.assertIn("RAM_MU_PER_GIB_HOUR", warnings[0])


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
        self.assertEqual(cpu_only, 2 * 4_000_000)
        self.assertEqual(disk_only, 100 * 100_000)

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


class StartCostTests(unittest.TestCase):
    """What starting an instance costs, and what the client gets for it."""

    def _cost(self, initial_balance_mu: int, build_mu: int = 10_000_000,
              free: bool = False) -> int:
        from src.utils.cost_functions import general_cost_functions as rates

        # Both patched in the namespace that calls them: `general_cost_functions` binds
        # these at import, so patching `execution_cost` would miss it.
        #
        # `is_free` is stubbed rather than driven through a threshold because it samples
        # the real machine when given no scarcity reading -- a load-dependent test would
        # pass or fail with whatever else is running. What the free tier does to a charge
        # is covered against explicit readings in FreeTierTests; what matters here is
        # only that the start cost is behind that guard at all.
        with patch.object(rates, "build_charge_mu", return_value=build_mu), \
             patch.object(rates, "is_free", return_value=free):
            return rates.compute_start_service_cost(
                metadata=celaut.Metadata(), initial_balance_mu=initial_balance_mu
            )

    def test_the_runtime_window_is_charged_exactly_once(self):
        """It used to be charged twice.

        The start charge added `INITIAL_RUNTIME_HOURS` of occupancy, and the instance was
        *also* funded with a balance derived from the same window -- so the client paid
        for two hours and got one, with the difference buying nothing. Everything charged
        here must either buy the build or become the instance's balance.
        """
        with _config():
            window_mu = execution_cost.maintenance_charge_mu(
                system_resources=_sysresources(mem_gib=0.25, vcpus=1, disk_gib=10),
                seconds=3600,
                scarcity=IDLE,
            )
            cost = self._cost(initial_balance_mu=window_mu)

        self.assertEqual(window_mu, 5_250_000)  # an hour of the documented instance
        self.assertEqual(cost, 10_000_000 + 5_250_000)

    def test_a_free_node_charges_nothing_to_start(self):
        with _config():
            self.assertEqual(self._cost(initial_balance_mu=5_250_000, free=True), 0)


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

    def test_a_curve_above_one_holds_the_surcharge_back(self):
        """The whole point of the setting, and the case nothing used to cover.

        Only the linear curve was ever tested, and linear is the one value at which the
        exponent and its reciprocal agree -- so the code raised `lack` to `1/curve` and
        made the surcharge arrive *earlier* as the curve rose, while five separate
        descriptions promised the opposite. At curve 2.0 a tenth of the supply gone must
        cost about 1.09x, not 3.85x.
        """
        with _config(**{"pricing.SCARCITY_MAX_MULTIPLIER": 10, "pricing.SCARCITY_CURVE": 2.0}):
            self.assertEqual(execution_cost.scarcity_bp(0.1), 10_900)
            self.assertEqual(execution_cost.scarcity_bp(0.5), 32_500)
            # The endpoints are fixed whatever the curve: no surcharge when plentiful,
            # the full ceiling when gone.
            self.assertEqual(execution_cost.scarcity_bp(0.0), 10_000)
            self.assertEqual(execution_cost.scarcity_bp(1.0), 100_000)

    def test_a_steeper_curve_never_charges_more_than_a_flatter_one(self):
        """The direction of the knob, stated as a property rather than as three numbers."""
        for lack in (0.1, 0.25, 0.5, 0.75, 0.9):
            with _config(**{"pricing.SCARCITY_CURVE": 1.0}):
                linear = execution_cost.scarcity_bp(lack)
            with _config(**{"pricing.SCARCITY_CURVE": 3.0}):
                steep = execution_cost.scarcity_bp(lack)
            self.assertLess(steep, linear, f"curve 3.0 overcharged at lack={lack}")

    def test_an_unreadable_machine_is_priced_as_scarce_not_free(self):
        """Failing open would give the node away; failing closed only refuses a client."""
        import psutil

        with patch("psutil.virtual_memory", side_effect=psutil.Error("boom")):
            scarcity = execution_cost.system_scarcity(force_refresh=True)
        self.assertEqual(scarcity, {"cpu": 1.0, "mem": 1.0, "disk": 1.0})


class FreeTierTests(unittest.TestCase):
    def test_a_zero_price_makes_that_resource_free(self):
        with _config(**{"pricing.RAM_MU_PER_GIB_HOUR": 0}):
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


class DerivedDepositTests(unittest.TestCase):
    def test_a_deposit_keeps_the_fee_under_the_configured_share(self):
        from src.payment_system import deposits
        from src.payment_system.contracts.ergo.interface import DEFAULT_FEE

        with _config(**{"deposits.MAX_FEE_OVERHEAD": 0.02, "deposits.REFILL_BELOW": 0.2}):
            full = deposits.full_deposit_mu()
            threshold = deposits.refill_threshold_mu()

        self.assertLessEqual(DEFAULT_FEE / full, 0.02)
        self.assertEqual(format_mu(full), "0.05 ERG")
        self.assertEqual(threshold, full // 5)

    def test_a_deposit_is_never_below_what_the_ledger_can_settle(self):
        """Ergo refuses an output under its minimum box value, so a deposit smaller
        than min_box + fee cannot be paid at all."""
        from src.payment_system import deposits
        from src.payment_system.contracts.ergo.interface import DEFAULT_FEE, SAFE_MIN_BOX_VALUE

        with _config(**{"deposits.MAX_FEE_OVERHEAD": 1.0}):
            self.assertGreaterEqual(deposits.full_deposit_mu(), SAFE_MIN_BOX_VALUE + DEFAULT_FEE)

    def test_the_floors_come_from_the_contracts_not_from_a_named_ledger(self):
        """Deposit sizing must not know which payment system it is sizing for.

        It used to import Ergo's `DEFAULT_FEE` and `SAFE_MIN_BOX_VALUE` directly, so
        Ergo's box-value floor applied to every contract — including the simulated one,
        whose payments never reach a chain. The floors now arrive through the same
        per-contract dispatch as the rest of the payment flow.
        """
        from src.payment_system import deposits

        # A contract that costs nothing to settle imposes no floor at all, so the only
        # thing left deciding the deposit is the fee-overhead rule against a zero fee.
        with _config(), patch.object(deposits, "_ledger_floors", return_value=(0, 0)):
            self.assertEqual(deposits.full_deposit_mu(), 0)

        # And a stricter ledger raises it, without deposits.py naming any of them.
        with _config(**{"deposits.MAX_FEE_OVERHEAD": 0.02}), \
             patch.object(deposits, "_ledger_floors", return_value=(2_000_000, 3_000_000)):
            self.assertEqual(deposits.full_deposit_mu(), 100_000_000)

    def test_the_strictest_contract_sets_the_floor(self):
        """One figure has to be payable on every available system.

        Deposit sizing runs before anyone has picked a contract, so it takes the maximum
        rather than the cheapest — a deposit sized for a free ledger would be refused by
        a chain that charges a fee.
        """
        from src.payment_system import deposits

        with _config(**{"deposits.MAX_FEE_OVERHEAD": 1.0}), \
             patch("src.payment_system.contracts.envs.settlement_floors", return_value={
                 "free-contract": lambda: (0, 0),
                 "costly-contract": lambda: (1_000_000, 1_000_000),
             }):
            self.assertEqual(deposits._ledger_floors(), (1_000_000, 1_000_000))
            self.assertEqual(deposits.full_deposit_mu(), 2_000_000)

    def test_a_simulated_contract_reports_no_settlement_floor(self):
        """The answer that makes the abstraction real rather than decorative."""
        from src.payment_system.contracts.simulator import interface as simulated

        self.assertEqual(simulated.settlement_floors_mu(), (0, 0))


if __name__ == "__main__":
    unittest.main()
