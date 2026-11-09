"""The price bound of a quote: ``price_bounded``, the volatility per period (#636).

A bounded quote promises that the price of an instance rises by at most ``(1 + v)`` per
period ``T`` from its launch: in the period ``k`` a tick charges at most the launch price
times ``(1 + v)^k``. These are the tests of the issue: the proto, the funding sum, the
ceiling by period, at_init and at_most, the fall of the load, the overcharge check, the
bound stored at the launch, the computed volatility, the config, the strict reading of a
peer, the selection of peers and the free node. The #335 tests, with v = 1.0 and T = 0,
are in tests/test_price_volatility.py.

The imports are not guarded, for the reason given in tests/test_pricing_model.py: a test
of money that is skipped says that the sums were checked when they were not.
"""
import math
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.balancers.estimated_cost_sorter import estimated_cost_sorter as sorter
from src.database.migrate import TABLES, create_tables
from src.database.sql_connection import SQLConnection
from src.manager import statement_check
from src.manager.maintain import _row_elapsed, _row_launch_bound, _row_launch_bp
from src.manager.manager import default_initial_balance
from src.manager.statement_check import MaintenanceQuote, Row
from src.utils.config import ConfigManager
from src.utils.config_validation import ConfigValidationError, validate_pricing_config
from src.utils.cost_functions import execution_cost, price_history
from src.utils.cost_functions.execution_cost import (
    CPU,
    DISK,
    MEM,
    launch_price_bp,
    maintenance_charge_mu,
    worst_case_charge_mu,
)
from src.utils.cost_functions.general_cost_functions import compute_start_service_cost
from src.utils.cost_functions.price_bound import (
    PriceBound,
    current_bound,
    funding_sum_mu,
    of_quote,
    strict,
)
from src.utils.cost_functions.start_funding import balance_after_build, minimum_runtime_mu
from src.utils.cost_functions.volatility_cost_normalization import (
    volatility_cost_normalization as vcnorm,
)
from src.utils.monetary import prices
from src.utils.utils import from_amount, to_amount

GIB = 1024 ** 3
HOUR = 3600
DAY = 24 * HOUR
IDLE = {CPU: 0.0, MEM: 0.0, DISK: 0.0}
FULL = {CPU: 1.0, MEM: 1.0, DISK: 1.0}

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
    "pricing.PRICE_BOUNDED": True,
    "pricing.PRICE_VOLATILITY": 0.10,
    "pricing.PRICE_VOLATILITY_PERIOD_HOURS": 24,
    "pricing.PRICE_VOLATILITY_WINDOW_HOURS": 168,
    "pricing.PRICE_VOLATILITY_MIN": 0.05,
    "free_tier.FREE_WHILE_SCARCITY_BELOW": 0.0,
    "deposits.INITIAL_RUNTIME_HOURS": 1.0,
}
TENTH_A_DAY = PriceBound(0.10, DAY)


def _config(**overrides):
    values = dict(BASE)
    values.update({key.replace("__", "."): value for key, value in overrides.items()})
    manager = ConfigManager()
    real_get = manager.get

    def get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    return patch.object(manager, "get", side_effect=get)


def _guest(mem=GIB) -> celaut.Sysresources:
    return celaut.Sysresources(mem_limit=mem, cpu_period=100000, cpu_quota=100000, disk_space=GIB)


def _tick(scarcity, launch_bp=None, bound=None, elapsed=None, guest=None) -> int:
    return maintenance_charge_mu(guest or _guest(), HOUR, scarcity=scarcity,
                                 launch_bp=launch_bp, bound=bound, elapsed_seconds=elapsed)


class TheProtoTests(unittest.TestCase):
    def test_the_fields_round_trip(self):
        for bounded in (True, False):
            with self.subTest(bounded=bounded):
                quote = celaut.EstimatedCost(price_bounded=bounded, price_volatility=0.25,
                                             volatility_period_seconds=86400)
                back = celaut.EstimatedCost.FromString(quote.SerializeToString())
                self.assertEqual(back.price_bounded, bounded)
                self.assertAlmostEqual(back.price_volatility, 0.25)
                self.assertEqual(back.volatility_period_seconds, 86400)

    def test_price_bounded_is_field_5_and_false_by_default(self):
        fields = celaut.EstimatedCost.DESCRIPTOR.fields_by_name
        self.assertEqual(fields["price_bounded"].number, 5)
        self.assertEqual(fields["price_volatility"].number, 6)
        self.assertEqual(fields["volatility_period_seconds"].number, 7)
        self.assertFalse(celaut.EstimatedCost().price_bounded)

    def test_no_variance_anywhere(self):
        self.assertNotIn("variance", celaut.EstimatedCost.DESCRIPTOR.fields_by_name)
        self.assertNotIn("variance", TABLES["delegated_instances"])
        self.assertNotIn("variance", MaintenanceQuote.__dataclass_fields__)
        import inspect

        self.assertNotIn("variance", inspect.signature(vcnorm).parameters)
        connection = sqlite3.connect(":memory:")
        create_tables(connection.cursor())
        columns = [row[1] for row in connection.execute("PRAGMA table_info(delegated_instances)")]
        self.assertNotIn("quote_variance", columns)
        for column in ("quote_price_bounded", "quote_price_volatility",
                       "quote_volatility_period_seconds"):
            self.assertIn(column, columns)

    def test_a_quote_carries_the_bound_of_the_config(self):
        from src.utils.cost_functions import generate_estimated_cost as module

        resources = celaut.Service.Container.Resources(at_init=_guest(), at_most=_guest(2 * GIB))
        with _config(), \
                patch.object(execution_cost, "system_scarcity", return_value=IDLE), \
                patch.object(module, "resolve_billable_resources", side_effect=lambda r, *a, **k: r), \
                patch.object(module, "compute_start_service_cost", return_value=0), \
                patch.object(module, "_service_hash_or_none", return_value=None), \
                patch.object(module, "MANAGER_ITERATION_TIME", HOUR):
            quote = module.generate_estimated_cost(
                metadata=celaut.Metadata(), config=celaut.Configuration(),
                resources=resources, check_availability=False)
            unbounded = None
            with _config(pricing__PRICE_BOUNDED=False):
                unbounded = module.generate_estimated_cost(
                    metadata=celaut.Metadata(), config=celaut.Configuration(),
                    resources=resources, check_availability=False)
            self.assertEqual(from_amount(quote.init_maintenance_cost), _tick(IDLE))
            self.assertEqual(from_amount(quote.max_maintenance_cost), _tick(IDLE, guest=_guest(2 * GIB)))
        self.assertTrue(quote.price_bounded)
        self.assertAlmostEqual(quote.price_volatility, 0.10, places=6)
        self.assertEqual(quote.volatility_period_seconds, DAY)
        self.assertFalse(unbounded.price_bounded)
        self.assertEqual(unbounded.price_volatility, 0.0)


class TheFundingSumTests(unittest.TestCase):
    def test_one_period_for_the_whole_life(self):
        self.assertEqual(funding_sum_mu(1000, 600, PriceBound(0.10, 0), 7), 1100 * 7)

    def test_each_period_uses_its_k(self):
        # Periods of 6 h with T = 24 h: k(i) = floor(i * 6 / 24) + 1.
        loop = 6 * HOUR
        expected = sum(1000 * 1.1 ** (math.floor(i * loop / DAY) + 1) for i in range(1, 9))
        self.assertEqual(funding_sum_mu(1000, loop, TENTH_A_DAY, 8), math.ceil(round(expected, 6)))
        # i = 4 ends at hour 24: it is already in the second period.
        self.assertEqual(funding_sum_mu(1000, loop, TENTH_A_DAY, 3), 3 * 1100)
        self.assertEqual(funding_sum_mu(1000, loop, TENTH_A_DAY, 4), 3 * 1100 + 1210)

    def test_no_bound_is_the_price_of_now(self):
        self.assertEqual(funding_sum_mu(1000, 600, None, 5), 5000)

    def test_the_cli_shows_the_same_sum(self):
        from src.commands.estimate import price_bound_lines

        quote = celaut.EstimatedCost(cost=to_amount(500), init_maintenance_cost=to_amount(1000),
                                     maintenance_seconds_loop=6 * HOUR, price_bounded=True,
                                     price_volatility=0.10, volatility_period_seconds=DAY)
        lines = price_bound_lines(quote, hours=(48,))
        self.assertIn("Bounded: yes", lines[1])
        total = 500 + funding_sum_mu(1000, 6 * HOUR, TENTH_A_DAY, 8)
        from src.utils.monetary import format_mu

        self.assertIn(format_mu(total), lines[2])


class TheCeilingByPeriodTests(unittest.TestCase):
    def test_a_tick_at_hour_30_is_bounded_by_the_second_period(self):
        with _config():
            launch = launch_price_bp(IDLE)
            at_launch = _tick(IDLE)
            self.assertEqual(_tick(FULL, launch, TENTH_A_DAY, elapsed=30 * HOUR),
                             _tick(IDLE, None, None) * 121 // 100)
            self.assertEqual(_tick(FULL, launch, TENTH_A_DAY, elapsed=2 * HOUR),
                             at_launch * 110 // 100)

    def test_the_ceiling_stops_at_the_maximum_multiplier(self):
        with _config():
            launch = launch_price_bp(IDLE)
            self.assertEqual(_tick(FULL, launch, TENTH_A_DAY, elapsed=10 ** 9), _tick(FULL))

    def test_period_zero_is_one_period(self):
        with _config():
            launch = launch_price_bp(IDLE)
            once = PriceBound(0.10, 0)
            self.assertEqual(_tick(FULL, launch, once, elapsed=10 * DAY),
                             _tick(FULL, launch, once, elapsed=0))

    def test_the_bound_applies_at_init_and_at_most_separately(self):
        # The instance was resized to twice its memory: each shape is held to its own
        # launch price times the ceiling.
        with _config():
            launch = launch_price_bp(IDLE)
            for guest in (_guest(), _guest(4 * GIB)):
                with self.subTest(mem=guest.mem_limit):
                    ceiling = _tick(IDLE, guest=guest) * 110 // 100
                    self.assertEqual(_tick(FULL, launch, TENTH_A_DAY, 0, guest=guest), ceiling)

    def test_a_fall_of_the_load_always_reaches_the_instance(self):
        with _config():
            launch = launch_price_bp(FULL)
            for elapsed in (0, 30 * HOUR, 30 * DAY):
                self.assertEqual(_tick(IDLE, launch, TENTH_A_DAY, elapsed), _tick(IDLE))

    def test_the_initial_balance_covers_the_hours_at_the_ceiling(self):
        with _config():
            # T >= INITIAL_RUNTIME_HOURS: just (1 + v).
            self.assertEqual(default_initial_balance(_guest(), scarcity=IDLE),
                             _tick(IDLE) * 110 // 100)
        with _config(pricing__PRICE_VOLATILITY_PERIOD_HOURS=0.5):
            # Two half hours: 1.1 and 1.21.
            half = maintenance_charge_mu(_guest(), HOUR / 2, scarcity=IDLE)
            self.assertEqual(default_initial_balance(_guest(), scarcity=IDLE),
                             half * 110 // 100 + half * 121 // 100)

    def test_no_tick_of_the_first_hours_exceeds_the_balance(self):
        with _config(pricing__PRICE_VOLATILITY_PERIOD_HOURS=0.25):
            launch = launch_price_bp(IDLE)
            bound = current_bound()
            balance = default_initial_balance(_guest(), scarcity=IDLE)
            spent = sum(maintenance_charge_mu(_guest(), 60, scarcity=FULL, launch_bp=launch,
                                              bound=bound, elapsed_seconds=60 * i - 1)
                        for i in range(1, 61))
            self.assertLessEqual(spent, balance)


class TheStoredBoundTests(unittest.TestCase):
    """The tick uses the volatility and the period stored at the launch."""

    ROW = {"mem_limit": GIB, "disk_space": GIB, "cpu_period": 100000, "cpu_quota": 100000,
           "launch_cpu_bp": 10000, "launch_mem_bp": 10000, "launch_disk_bp": 10000,
           "launch_volatility": 0.10, "launch_period_seconds": DAY}

    def _sweep(self, row, scarcity, **config):
        from src.manager import maintain

        spend = MagicMock(return_value=True)
        with _config(**config), \
                patch.object(maintain.sc, "get_running_internal_containers_ids", return_value=["vm-1"]), \
                patch.object(maintain.sc, "get_sys_req", return_value=row), \
                patch.object(maintain.sc, "record_instance_consumption"), \
                patch.object(maintain, "vm_maintain"), \
                patch.object(maintain, "spend_mu", spend), \
                patch.object(maintain, "system_scarcity", return_value=scarcity), \
                patch.object(maintain, "price_history"), \
                patch.object(maintain, "_reputation_interface"), \
                patch.object(maintain, "vm_janitor_cleanup_orphans"), \
                patch.object(maintain, "MANAGER_ITERATION_TIME", HOUR):
            maintain.maintain_vmachines(debug_mode=False)
        return spend.call_args.kwargs["amount_mu"]

    def test_the_tick_uses_the_launch_values_not_the_config_of_now(self):
        import time

        with _config():
            at_launch = _tick(IDLE)
        row = dict(self.ROW, launched_ts=int(time.time()) - 30 * HOUR)
        # The config of now says 3.0 per hour, and unbounded: the row says 0.10 per day.
        for config in ({"pricing__PRICE_VOLATILITY": 3.0, "pricing__PRICE_VOLATILITY_PERIOD_HOURS": 1},
                       {"pricing__PRICE_BOUNDED": False}):
            with self.subTest(config=config):
                self.assertEqual(self._sweep(row, FULL, **config), at_launch * 121 // 100)

    def test_the_launch_stores_what_it_read(self):
        import src.gateway.launcher.local_execution.local_execution as local_execute
        from tests.test_price_volatility import TheWiringTests

        read = PriceBound(0.07, 0)
        with patch.object(local_execute, "current_bound", return_value=read) as current:
            recorded = TheWiringTests()._launch(IDLE)
        current.assert_called_once_with()
        self.assertEqual(recorded["launch_bound"], read)
        with _config():
            self.assertEqual(recorded["balance_mu"], _tick(IDLE) * 107 // 100)

    def test_rows_read_strictly(self):
        self.assertEqual(_row_launch_bound({"launch_volatility": float("nan"),
                                            "launch_period_seconds": -5}), PriceBound(0.0, 0))
        self.assertIsNone(_row_launch_bound({"launch_volatility": None,
                                             "launch_period_seconds": 0}))
        self.assertIsNone(_row_elapsed({"launched_ts": None}))
        self.assertAlmostEqual(_row_elapsed({"launched_ts": 100}, now=160), 60)


class TheComputedVolatilityTests(unittest.TestCase):
    @staticmethod
    def _samples(values, step=HOUR, start=0):
        return [(start + i * step, {"cpu": v, "mem": 10000, "disk": 10000})
                for i, v in enumerate(values)]

    def test_the_p95_of_the_rises_per_stretch(self):
        # 40 hourly samples: flat at 1x, one jump to 1.5x at hour 20. With T = 1 h, one
        # stretch of 39 holds the jump and rises 50 %: fewer than 5 %, so the p95 is 0.
        values = [10000] * 20 + [15000] * 20
        samples = self._samples(values)
        rises = price_history.rises(samples, HOUR)
        self.assertEqual(len(rises), 39)
        self.assertEqual(sorted(rises)[-2:], [0.0, 0.5])
        self.assertEqual(price_history.volatility_of(samples, HOUR, floor=0.0), 0.0)
        # With T = 2 h two stretches of 38 hold it (5.3 %): the p95 is the jump.
        self.assertEqual(price_history.volatility_of(samples, 2 * HOUR, floor=0.0), 0.5)
        # A rise in every second stretch: the p95 is that rise.
        jumpy = self._samples([10000, 12000] * 20)
        self.assertAlmostEqual(price_history.volatility_of(jumpy, HOUR, floor=0.0), 0.2)

    def test_a_fall_counts_as_zero(self):
        samples = self._samples([20000 - 100 * i for i in range(30)])
        self.assertEqual(price_history.rises(samples, HOUR), [0.0] * 29)

    def test_the_floor_applies(self):
        jumpy = self._samples([10000, 10100] * 20)
        self.assertEqual(price_history.volatility_of(jumpy, HOUR, floor=0.05), 0.05)

    def test_no_history_or_a_short_one_is_the_floor(self):
        self.assertEqual(price_history.volatility_of([], DAY, floor=0.05), 0.05)
        short = self._samples([10000, 30000, 90000])
        self.assertEqual(price_history.volatility_of(short, DAY, floor=0.05), 0.05)
        self.assertEqual(price_history.volatility_of(short, 0, floor=0.05), 0.05)

    def test_always_finite_and_not_negative(self):
        for floor in (float("nan"), -1.0, 0.0):
            value = price_history.volatility_of(self._samples([10000] * 5), HOUR, floor)
            self.assertTrue(math.isfinite(value) and value >= 0)

    def test_from_the_database(self):
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        original = SQLConnection._connection
        SQLConnection._connection = connection
        try:
            sc = SQLConnection()
            now = 10 * DAY
            for i, v in enumerate([10000, 12000] * 50):
                sc.add_price_sample(now - 100 * HOUR + i * HOUR,
                                    {"cpu": v, "mem": 10000, "disk": 10000}, keep_seconds=7 * DAY)
            self.assertAlmostEqual(price_history.computed_volatility(
                HOUR, 7 * DAY, 0.05, now=now, db=sc), 0.2)
            # The window: nothing in the last half hour.
            self.assertEqual(price_history.computed_volatility(
                HOUR, 1800, 0.05, now=now + 2 * HOUR, db=sc), 0.05)
        finally:
            SQLConnection._connection = original
            connection.close()

    def test_the_config_without_a_value_computes_it(self):
        with _config(pricing__PRICE_VOLATILITY=None), \
                patch.object(price_history, "computed_volatility", return_value=0.33) as computed:
            self.assertEqual(current_bound(), PriceBound(0.33, DAY))
        computed.assert_called_once_with(period_seconds=DAY, window_seconds=168 * HOUR, floor=0.05)


class TheConfigTests(unittest.TestCase):
    def test_invalid_values_refuse_to_load(self):
        for key in ("PRICE_VOLATILITY", "PRICE_VOLATILITY_PERIOD_HOURS",
                    "PRICE_VOLATILITY_WINDOW_HOURS", "PRICE_VOLATILITY_MIN"):
            for bad in (float("nan"), float("inf"), -1, "x", True):
                with self.subTest(key=key, bad=bad), self.assertRaises(ConfigValidationError):
                    validate_pricing_config({"pricing": {key: bad}})
        with self.assertRaises(ConfigValidationError):
            validate_pricing_config({"pricing": {"PRICE_VOLATILITY_WINDOW_HOURS": 0}})
        with self.assertRaises(ConfigValidationError):
            validate_pricing_config({"pricing": {"PRICE_BOUNDED": "yes"}})

    def test_the_removed_key_is_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "MAX_PRICE_VOLATILITY"):
            validate_pricing_config({"pricing": {"MAX_PRICE_VOLATILITY": 2.0}})

    def test_valid_values_load(self):
        validate_pricing_config({"pricing": {
            "PRICE_BOUNDED": False, "PRICE_VOLATILITY": 0, "PRICE_VOLATILITY_PERIOD_HOURS": 0,
            "PRICE_VOLATILITY_WINDOW_HOURS": 0.5, "PRICE_VOLATILITY_MIN": 0}})
        validate_pricing_config({"pricing": {"PRICE_VOLATILITY": None}})

    def test_prices_refuses_them_too(self):
        with _config(pricing__PRICE_VOLATILITY_MIN=float("inf")):
            with self.assertRaisesRegex(ValueError, "PRICE_VOLATILITY_MIN"):
                prices()
        with _config(pricing__PRICE_BOUNDED="no"):
            with self.assertRaisesRegex(ValueError, "PRICE_BOUNDED"):
                prices()

    def test_the_window_defaults_to_seven_periods(self):
        values = {k: v for k, v in BASE.items() if k != "pricing.PRICE_VOLATILITY_WINDOW_HOURS"}
        values["pricing.PRICE_VOLATILITY_PERIOD_HOURS"] = 2
        manager = ConfigManager()
        real_get = manager.get

        def get(key, default=None):
            if key == "pricing.PRICE_VOLATILITY_WINDOW_HOURS":
                return default
            return values[key] if key in values else real_get(key, default)

        with patch.object(manager, "get", side_effect=get):
            self.assertEqual(prices().volatility_window_seconds, 14 * HOUR)


class TheStrictReadingTests(unittest.TestCase):
    def test_invalid_values_of_a_peer(self):
        for v in (float("nan"), float("inf"), -0.5, "x", None):
            with self.subTest(v=v):
                self.assertEqual(strict(v, 60).volatility, 0.0)
        self.assertEqual(strict(0.1, -1).period_seconds, 0)
        self.assertEqual(strict(0.10000000149011612, 60), PriceBound(0.1, 60))

    def test_of_row_reads_strictly(self):
        row = {"quote_price_bounded": 1, "quote_max_maintenance_cost": 100,
               "quote_init_maintenance_cost": 60, "quote_maintenance_seconds_loop": 10,
               "quote_price_volatility": float("nan"), "quote_volatility_period_seconds": -3}
        self.assertEqual(MaintenanceQuote.of_row(row).bound, PriceBound(0.0, 0))

    def test_of_quote(self):
        self.assertIsNone(of_quote(celaut.EstimatedCost(price_volatility=5.0)))
        quote = celaut.EstimatedCost(price_bounded=True, price_volatility=float("inf"),
                                     volatility_period_seconds=-1)
        self.assertEqual(of_quote(quote), PriceBound(0.0, 0))

    def test_vcnorm_does_not_raise(self):
        for v in (float("nan"), float("inf"), -1.0, None, "x", 1e308):
            with self.subTest(v=v):
                self.assertIsInstance(vcnorm(10 ** 6, v), int)
        self.assertEqual(vcnorm(1000, float("inf")), 1000)
        self.assertEqual(vcnorm(1000, 0.5), 1500)
        self.assertGreater(vcnorm(10 ** 6, 1e308), 10 ** 6)


class TheSelectionTests(unittest.TestCase):
    @staticmethod
    def _cost(mu, bounded, volatility=0.0):
        return celaut.EstimatedCost(cost=to_amount(mu), init_maintenance_cost=to_amount(0),
                                    max_maintenance_cost=to_amount(0), maintenance_seconds_loop=3600,
                                    price_bounded=bounded, price_volatility=volatility)

    def _order(self, candidates, **config):
        with _config(**config), \
                patch.object(sorter, "bonus_by_peer", return_value={}), \
                patch.object(sorter, "standing_by_peer", return_value={}), \
                patch.object(sorter, "compute_reputation", return_value=0.0):
            return [peer_id for peer_id, _ in sorter.estimated_cost_sorter(candidates)]

    def test_bounded_ahead_of_unbounded_cost_order_inside_each_group(self):
        candidates = {
            "cheap-unbounded": self._cost(100, False),
            "dear-bounded": self._cost(5000, True),
            "cheap-bounded": self._cost(1000, True),
            "dear-unbounded": self._cost(9000, False),
        }
        self.assertEqual(self._order(candidates),
                         ["cheap-bounded", "dear-bounded", "cheap-unbounded", "dear-unbounded"])

    def test_the_volatility_weighs_inside_the_group(self):
        candidates = {"calm": self._cost(1000, True, 0.0), "wild": self._cost(900, True, 0.5)}
        self.assertEqual(self._order(candidates), ["calm", "wild"])

    def test_the_local_node_is_bounded_iff_price_bounded(self):
        candidates = {"local": self._cost(10 ** 9, False), "peer": self._cost(1, False)}
        self.assertEqual(self._order(candidates)[0], "local")
        self.assertEqual(self._order(candidates, pricing__PRICE_BOUNDED=False)[0], "peer")


class TheOverchargeTests(unittest.TestCase):
    QUOTE = {"quote_init_maintenance_cost": 60, "quote_max_maintenance_cost": 100,
             "quote_maintenance_seconds_loop": 10, "quote_price_bounded": 1,
             "quote_price_volatility": 0.10, "quote_volatility_period_seconds": 1000,
             "launched_ts": 1000}
    START = {"mem_limit": GIB, "disk_space": GIB, "cpu_period": 100000, "cpu_quota": 100000}

    def _rows(self, *periods, start_ts=1000, shape=None):
        """One maintenance movement of 10 s per amount, at ``start_ts + 10 * i``."""
        return [Row(seq=3 + i, ts=start_ts + 10 * (i + 1), amount=-amount, balance_after=0,
                    kind="maintenance", ref="", detail=dict(shape or self.START, seconds=10))
                for i, amount in enumerate(periods)]

    def test_unbounded_is_never_an_overcharge(self):
        quote = MaintenanceQuote.of_row(dict(self.QUOTE, quote_price_bounded=0))
        self.assertIsNone(quote)
        self.assertIsNone(statement_check.maintenance_overcharge(
            self._rows(10 ** 12), quote, 1000))

    def test_bounded_is_scored_above_the_exact_ceiling(self):
        quote = MaintenanceQuote.of_row(self.QUOTE)
        # First period: 100 * 1.1 = 110, plus 1 MU of rounding.
        self.assertIsNone(statement_check.maintenance_overcharge(self._rows(111), quote, 1000))
        self.assertIsNotNone(statement_check.maintenance_overcharge(self._rows(112), quote, 1000))

    def test_the_period_of_the_movement_counts(self):
        quote = MaintenanceQuote.of_row(self.QUOTE)
        # Dated 1880 s after the launch, with the 120 s of clock margin: k = 3, so
        # 100 * 1.331 + 1 MU.
        self.assertIsNone(statement_check.maintenance_overcharge(
            self._rows(134, start_ts=2870), quote, 2870))
        self.assertIsNotNone(statement_check.maintenance_overcharge(
            self._rows(135, start_ts=2870), quote, 2870))

    def test_the_figure_of_the_resources_held(self):
        row = dict(self.QUOTE, **{f"peer_base_{k}": v for k, v in self.START.items()})
        quote = MaintenanceQuote.of_row(row)
        # At the start shape: 60 * 1.1 = 66, + 1.
        self.assertIsNone(statement_check.maintenance_overcharge(self._rows(67), quote, 1000))
        self.assertIsNotNone(statement_check.maintenance_overcharge(self._rows(68), quote, 1000))
        # Grown past it: the figure at the most, 110 + 1.
        grown = dict(self.START, mem_limit=2 * GIB)
        self.assertIsNone(statement_check.maintenance_overcharge(
            self._rows(111, shape=grown), quote, 1000))
        self.assertIsNotNone(statement_check.maintenance_overcharge(
            self._rows(112, shape=grown), quote, 1000))

    def test_no_launch_time_with_a_period_is_not_checked(self):
        quote = MaintenanceQuote.of_row(dict(self.QUOTE, launched_ts=None))
        self.assertIsNone(statement_check.maintenance_overcharge(self._rows(10 ** 6), quote, 1000))


class TheFreeNodeTests(unittest.TestCase):
    FREE = {"free_tier__FREE_WHILE_SCARCITY_BELOW": 0.5}

    def test_the_quote_is_not_zero(self):
        from src.utils.cost_functions import generate_estimated_cost as module

        resources = celaut.Service.Container.Resources(at_init=_guest(), at_most=_guest())
        with _config(**self.FREE), \
                patch.object(execution_cost, "system_scarcity", return_value=IDLE), \
                patch.object(module, "resolve_billable_resources", side_effect=lambda r, *a, **k: r), \
                patch.object(module, "_service_hash_or_none", return_value=None), \
                patch.object(module, "MANAGER_ITERATION_TIME", HOUR):
            quote = module.generate_estimated_cost(
                metadata=celaut.Metadata(), config=celaut.Configuration(initial_mu=to_amount(777)),
                resources=resources, check_availability=False)
        with _config():
            base = _tick(IDLE)
        self.assertEqual(from_amount(quote.max_maintenance_cost), base)
        self.assertEqual(from_amount(quote.init_maintenance_cost), base)
        # The build is free, the balance is not.
        self.assertEqual(from_amount(quote.cost), 777)

    def test_a_free_launch_is_funded_the_ceiling_and_survives_leaving_the_free_state(self):
        with _config(**self.FREE, costs__ALLOW_DEBT=False):
            scarcity = IDLE
            launch = launch_price_bp(scarcity)
            bound = current_bound()
            balance = default_initial_balance(_guest(), scarcity=scarcity, bound=bound)
            resources = celaut.Service.Container.Resources(at_init=_guest())
            minimum = minimum_runtime_mu(resources, scarcity=scarcity, bound=bound)
            with patch.object(execution_cost, "is_free", return_value=True), \
                    patch("src.utils.cost_functions.general_cost_functions.is_free", return_value=True):
                cost = compute_start_service_cost(celaut.Metadata(), balance)
            self.assertEqual(launch, {CPU: 10000, MEM: 10000, DISK: 10000})
            self.assertEqual(cost, balance)
            self.assertEqual(balance_after_build(cost, 0, minimum), balance)
            # Free: ticks charge 0. Then the node fills up: every tick of the first hour
            # is at the ceiling over the anchor, and the balance pays all of them.
            self.assertEqual(maintenance_charge_mu(_guest(), 60, scarcity=IDLE, launch_bp=launch,
                                                   bound=bound, elapsed_seconds=60), 0)
            left = balance
            for i in range(1, 61):
                left -= maintenance_charge_mu(_guest(), 60, scarcity=FULL, launch_bp=launch,
                                              bound=bound, elapsed_seconds=60 * i)
            self.assertGreaterEqual(left, 0)
        with _config():
            self.assertEqual(balance, _tick(IDLE) * 110 // 100)


if __name__ == "__main__":
    unittest.main()
