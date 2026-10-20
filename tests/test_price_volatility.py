"""The price of a running instance may rise only so far above its launch price (#335).

Scarcity is read from the load of the moment, so without a bound an instance funded at
1x could be charged at 10x, run out of balance and be stopped for a price it never saw.
`pricing.MAX_PRICE_VOLATILITY` bounds the rise. These tests cover the clamp, that the
quote and the funding come from the same figure as the tick, the free launch, and the
columns that record the launch price.

The imports are not guarded, for the reason given in tests/test_pricing_model.py: a
test of money that is skipped says that the sums were checked when they were not.
"""

import sqlite3
import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.database.migrate import create_tables
from src.database.sql_connection import SQLConnection
from src.manager.maintain import _row_launch_bp
from src.manager.manager import default_initial_balance
from src.utils.config import ConfigManager
from src.utils.config_validation import ConfigValidationError, validate_pricing_config
from src.utils.cost_functions import execution_cost
from src.utils.cost_functions.execution_cost import (
    CPU,
    DISK,
    MEM,
    launch_price_bp,
    maintenance_charge_mu,
    worst_case_charge_mu,
)
from src.utils.monetary import prices
from src.utils.utils import from_amount

GIB = 1024 ** 3
HOUR = 3600
IDLE = {CPU: 0.0, MEM: 0.0, DISK: 0.0}
FULL = {CPU: 1.0, MEM: 1.0, DISK: 1.0}
HALF = {CPU: 0.5, MEM: 0.5, DISK: 0.5}

BASE = {
    "pricing.RAM_MU_PER_GIB_HOUR": 1_000_000,
    "pricing.CPU_MU_PER_VCPU_HOUR": 4_000_000,
    "pricing.DISK_MU_PER_GIB_HOUR": 100_000,
    "pricing.NET_MU_PER_GIB": 2_000_000,
    "pricing.BUILD_MU": 10_000_000,
    "pricing.TUNNEL_OPEN_MU": 10_000,
    "pricing.MODIFY_RESOURCES_MU": 10_000,
    "pricing.SCARCITY_MAX_MULTIPLIER": 10,
    "pricing.SCARCITY_CURVE": 1.0,
    "pricing.MAX_PRICE_VOLATILITY": 2.0,
    "free_tier.FREE_WHILE_SCARCITY_BELOW": 0.0,
    "deposits.INITIAL_RUNTIME_HOURS": 1.0,
}


def _config(**overrides):
    values = dict(BASE)
    values.update({key.replace("__", "."): value for key, value in overrides.items()})
    manager = ConfigManager()
    real_get = manager.get

    def get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    return patch.object(manager, "get", side_effect=get)


def _guest() -> celaut.Sysresources:
    return celaut.Sysresources(mem_limit=GIB, cpu_period=100000, cpu_quota=100000, disk_space=GIB)


def _tick(scarcity, launch_bp=None) -> int:
    return maintenance_charge_mu(_guest(), HOUR, scarcity=scarcity, launch_bp=launch_bp)


class TheKeyTests(unittest.TestCase):
    def test_the_bound_is_read(self):
        with _config():
            self.assertEqual(prices().volatility_bound(), 2.0)

    def test_zero_means_no_bound(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=0):
            self.assertIsNone(prices().volatility_bound())

    def test_a_value_from_the_maximum_multiplier_up_means_no_bound(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=10):
            self.assertIsNone(prices().volatility_bound())
        with _config(pricing__MAX_PRICE_VOLATILITY=9.5):
            self.assertEqual(prices().volatility_bound(), 9.5)

    def test_a_value_between_zero_and_one_is_refused(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=0.5):
            with self.assertRaisesRegex(ValueError, "MAX_PRICE_VOLATILITY"):
                prices()

    def test_the_config_check_takes_what_prices_takes(self):
        for good in (0, 1, 1.0, 2, 2.5, 10):
            validate_pricing_config({"pricing": {"MAX_PRICE_VOLATILITY": good}})

    def test_the_config_check_refuses_the_rest(self):
        for bad in (0.5, -1, "fast", True, None, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ConfigValidationError):
                validate_pricing_config({"pricing": {"MAX_PRICE_VOLATILITY": bad}})


class TheClampTests(unittest.TestCase):
    def test_a_rise_reaches_the_instance_only_up_to_the_bound(self):
        with _config():
            launch = launch_price_bp(IDLE)
            at_launch = _tick(IDLE, launch)
            # The node is full, which is 10x unbounded. The instance pays 2x.
            self.assertEqual(_tick(FULL, launch), 2 * at_launch)
            self.assertEqual(_tick(FULL), 10 * at_launch)

    def test_a_rise_below_the_bound_passes_whole(self):
        with _config():
            launch = launch_price_bp(IDLE)
            # Half full is 5.5x unbounded: over the bound. A small rise is below it.
            slight = {CPU: 0.05, MEM: 0.05, DISK: 0.05}
            self.assertEqual(_tick(slight, launch), _tick(slight))

    def test_a_fall_reaches_the_instance_in_full(self):
        # Ceiling only: launched at a loaded node, an empty node is cheaper for it.
        with _config():
            launch = launch_price_bp(FULL)
            self.assertEqual(_tick(IDLE, launch), _tick(IDLE))
            self.assertLess(_tick(IDLE, launch), _tick(FULL, launch))

    def test_the_bound_is_of_the_launch_price_not_of_the_idle_price(self):
        with _config():
            launch = launch_price_bp(HALF)
            at_launch = _tick(HALF, launch)
            # 5.5x launch, times 2, is over the maximum: the maximum of 10x is the limit.
            self.assertEqual(_tick(FULL, launch), _tick(FULL))
            self.assertGreater(_tick(FULL, launch), at_launch)

    def test_a_bound_of_one_fixes_the_price(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=1.0):
            launch = launch_price_bp(HALF)
            self.assertEqual(_tick(FULL, launch), _tick(HALF, launch))

    def test_the_bound_works_per_resource(self):
        with _config():
            launch = launch_price_bp({CPU: 0.0, MEM: 0.0, DISK: 0.0})
            memory_only = {CPU: 0.0, MEM: 1.0, DISK: 0.0}
            idle = _tick(IDLE, launch)
            ram = 1_000_000  # one GiB for one hour, at 1x
            # Only memory rose; it is held at 2x, and the other two stay at 1x.
            self.assertEqual(_tick(memory_only, launch), idle + ram)

    def test_no_bound_is_the_old_behaviour(self):
        for off in (0, 10):
            with self.subTest(value=off), _config(pricing__MAX_PRICE_VOLATILITY=off):
                launch = launch_price_bp(IDLE)
                self.assertEqual(_tick(FULL, launch), _tick(FULL))

    def test_a_row_with_no_launch_price_is_not_bounded(self):
        with _config():
            self.assertEqual(_tick(FULL, None), _tick(FULL))

    def test_an_instance_launched_free_stays_free(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5):
            launch = launch_price_bp(IDLE)
            self.assertEqual(launch, {CPU: 0, MEM: 0, DISK: 0})
            self.assertEqual(_tick(IDLE, launch), 0)
            # The node crosses the threshold, and the instance still pays nothing.
            self.assertEqual(_tick(FULL, launch), 0)
            self.assertGreater(_tick(FULL), 0)

    def test_a_free_launch_is_charged_when_there_is_no_bound(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5, pricing__MAX_PRICE_VOLATILITY=0):
            launch = launch_price_bp(IDLE)
            self.assertGreater(_tick(FULL, launch), 0)


class TheQuoteAndTheFundingTests(unittest.TestCase):
    def test_the_worst_case_is_what_no_tick_can_exceed(self):
        with _config():
            for load in (IDLE, HALF, FULL):
                with self.subTest(load=load):
                    launch = launch_price_bp(load)
                    worst = worst_case_charge_mu(_guest(), HOUR, scarcity=load)
                    for later in (IDLE, HALF, FULL):
                        self.assertLessEqual(_tick(later, launch), worst)

    def test_the_worst_case_of_an_idle_launch_is_twice_its_price(self):
        with _config():
            self.assertEqual(
                worst_case_charge_mu(_guest(), HOUR, scarcity=IDLE), 2 * _tick(IDLE)
            )

    def test_the_worst_case_of_a_loaded_launch_stops_at_the_maximum(self):
        with _config():
            self.assertEqual(
                worst_case_charge_mu(_guest(), HOUR, scarcity=FULL), _tick(FULL)
            )

    def test_with_no_bound_the_worst_case_is_the_charge_now(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=0):
            self.assertEqual(worst_case_charge_mu(_guest(), HOUR, scarcity=HALF), _tick(HALF))

    def test_a_free_node_has_no_worst_case(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5):
            self.assertEqual(worst_case_charge_mu(_guest(), HOUR, scarcity=IDLE), 0)

    def test_the_initial_balance_funds_the_hours_at_the_worst_case(self):
        with _config():
            balance = default_initial_balance(_guest(), scarcity=IDLE)
            self.assertEqual(balance, 2 * _tick(IDLE))
            launch = launch_price_bp(IDLE)
            # The balance lasts the hour even if the node fills up at once.
            self.assertGreaterEqual(balance, _tick(FULL, launch))

    def test_the_initial_balance_is_unchanged_with_no_bound(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=0):
            self.assertEqual(default_initial_balance(_guest(), scarcity=IDLE), _tick(IDLE))

    def test_an_instance_launched_free_is_funded_nothing(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5):
            self.assertEqual(default_initial_balance(_guest(), scarcity=IDLE), 0)

    def test_the_quote_of_the_most_a_tick_charges_is_bounded(self):
        from src.utils.cost_functions import generate_estimated_cost as module

        resources = celaut.Service.Container.Resources()
        resources.at_init.CopyFrom(_guest())
        resources.at_most.CopyFrom(_guest())
        with _config(), \
                patch.object(execution_cost, "system_scarcity", return_value=IDLE), \
                patch.object(module, "resolve_billable_resources", side_effect=lambda r, *a, **k: r), \
                patch.object(module, "compute_start_service_cost", return_value=0), \
                patch.object(module, "_service_hash_or_none", return_value=None), \
                patch.object(module, "MANAGER_ITERATION_TIME", HOUR):
            quote = module.generate_estimated_cost(
                metadata=celaut.Metadata(), config=celaut.Configuration(),
                resources=resources, check_availability=False,
            )
        self.assertEqual(
            from_amount(quote.max_maintenance_cost), 2 * from_amount(quote.init_maintenance_cost)
        )


class TheRecordTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def _add(self, instance_id, **extra):
        self.sc.add_local_instance(
            father_id="father", container_ip="10.0.0.2", container_id=instance_id,
            name=instance_id, balance_mu=100, serialized_instance="x", service_id="svc",
            virtualizer="ch", disk_space=0, envs="{}", **extra,
        )

    def test_the_launch_price_is_recorded_and_read_back(self):
        self._add("a", launch_bp={CPU: 10000, MEM: 25000, DISK: 0})
        self.assertEqual(
            _row_launch_bp(self.sc.get_sys_req("a")), {CPU: 10000, MEM: 25000, DISK: 0}
        )

    def test_no_launch_price_reads_as_no_bound(self):
        self._add("a")
        self.assertIsNone(_row_launch_bp(self.sc.get_sys_req("a")))

    def test_a_row_without_the_columns_reads_as_no_bound(self):
        self.assertIsNone(_row_launch_bp({"arch": None}))

    def test_an_old_table_gets_the_columns(self):
        from src.database.migrate import ensure_columns

        connection = sqlite3.connect(":memory:")
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE local_instances (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO local_instances (id) VALUES ('old')")
        ensure_columns(cursor, "local_instances", {
            "launch_cpu_bp": "INTEGER DEFAULT NULL",
            "launch_mem_bp": "INTEGER DEFAULT NULL",
            "launch_disk_bp": "INTEGER DEFAULT NULL",
        })
        row = cursor.execute("SELECT launch_cpu_bp, launch_mem_bp, launch_disk_bp FROM local_instances").fetchone()
        self.assertEqual(row, (None, None, None))


if __name__ == "__main__":
    unittest.main()
