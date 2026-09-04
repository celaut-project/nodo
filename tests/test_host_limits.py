"""`host_limits`: the ceilings on what this node may take from its host.

These ceilings are the only thing in the node that refuses a workload for being too
large: `pricing.SCARCITY_*` only makes a loaded machine expensive and `low_demand` only
gates the opportunistic fallback, so with the ceilings lifted a client may rent every
core and byte the host has. They are what says no on behalf of the person sitting at the
PC, which is why the tests below are mostly about *refusing* correctly.

What each group pins:

* **Resolving shares.** A share is of the whole machine, so the ceiling depends on the
  machine -- and a total psutil cannot report has to lift the ceiling rather than close
  it, because an unknown capacity is not evidence of a small one.
* **Adding up what is held.** Against the granted figures in `local_instances`, which is
  what makes the sum a bound rather than a guess.
* **Every breach, not the first.** An operator told only about memory raises the memory
  share, retries, and is then told about disk.
* **The daily traffic counter.** It has to survive a restart: an allowance that reset
  with the daemon would not be a daily one.
* **The rate limiter.** One bucket for the whole node, or ten tunnels would each get the
  configured rate.
"""
import sys
import types
import unittest
from datetime import date
from unittest import mock

IMPORT_ERROR = None
try:
    from src.utils import host_limits
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    host_limits = None  # type: ignore[assignment]

RESIZE_IMPORT_ERROR = None
try:
    from src.manager import modify_resources
except Exception as import_exc:  # pragma: no cover - environment-dependent
    RESIZE_IMPORT_ERROR = import_exc
    modify_resources = None  # type: ignore[assignment]

GIB = 1024 ** 3
MIB = 1024 ** 2


def _row(mem=0, disk=0, quota=0, period=100_000):
    return {"mem_limit": mem, "disk_space": disk, "cpu_quota": quota, "cpu_period": period}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HostLimitsTestCase(unittest.TestCase):
    """Shared plumbing: a config, and a machine of a known size."""

    #: 8 cores, 16 GiB of RAM, 500 GiB of storage.
    CORES = 8
    RAM = 16 * GIB
    DISK = 500 * GIB

    def _configured(self, **settings):
        values = {f"host_limits.{key}": value for key, value in settings.items()}
        values.setdefault("main.STORAGE", "/nodo/storage")
        return mock.patch.object(
            host_limits.env_manager,
            "get",
            side_effect=lambda key, default=None: values.get(key, default),
        )

    #: Distinguishes "this test did not say" from "this test said None", which is the
    #: reading psutil gives for a capacity it cannot report and a case with its own test.
    UNSET = object()

    def _machine(self, cores=UNSET, ram=UNSET, disk=UNSET):
        return mock.patch.object(
            host_limits,
            "host_totals",
            return_value=(
                self.CORES if cores is self.UNSET else cores,
                self.RAM if ram is self.UNSET else ram,
                self.DISK if disk is self.UNSET else disk,
            ),
        )

    def setUp(self):
        # Network settings are cached for a second; each test configures its own.
        host_limits._net_settings = None


class ResolvingSharesTests(HostLimitsTestCase):

    def test_a_node_with_the_section_off_has_no_ceilings(self):
        with self._configured(MAX_RAM_SHARE=0.5), self._machine():
            self.assertIsNone(host_limits.ceilings())

    def test_shares_resolve_against_what_the_machine_actually_has(self):
        with self._configured(ENABLED=True, MAX_CPU_SHARE=0.5, MAX_RAM_SHARE=0.25,
                              MAX_DISK_SHARE=0.1), self._machine():
            bounds = host_limits.ceilings()
        self.assertEqual(bounds.cores, 4.0)
        self.assertEqual(bounds.ram_bytes, 4 * GIB)
        self.assertEqual(bounds.disk_bytes, 50 * GIB)

    def test_a_share_of_zero_lifts_that_ceiling_and_leaves_the_others(self):
        with self._configured(ENABLED=True, MAX_CPU_SHARE=0, MAX_RAM_SHARE=0.5,
                              MAX_DISK_SHARE=0), self._machine():
            bounds = host_limits.ceilings()
        self.assertIsNone(bounds.cores)
        self.assertEqual(bounds.ram_bytes, 8 * GIB)
        self.assertIsNone(bounds.disk_bytes)

    def test_every_share_at_zero_is_the_same_as_no_ceilings_at_all(self):
        with self._configured(ENABLED=True, MAX_CPU_SHARE=0, MAX_RAM_SHARE=0,
                              MAX_DISK_SHARE=0), self._machine():
            self.assertIsNone(host_limits.ceilings())

    def test_a_capacity_psutil_cannot_report_lifts_its_ceiling(self):
        """An unknown total is not evidence of a small one.

        Refusing every launch because `cpu_count` came back None would turn a reading
        failure into an outage, and the memory pool and free-disk checks in
        `resource_availability` are still there either way.
        """
        with self._configured(ENABLED=True, MAX_CPU_SHARE=0.5, MAX_RAM_SHARE=0.5,
                              MAX_DISK_SHARE=0.5), self._machine(cores=None):
            bounds = host_limits.ceilings()
        self.assertIsNone(bounds.cores)
        self.assertEqual(bounds.ram_bytes, 8 * GIB)

    def test_a_share_above_one_is_the_whole_machine_rather_than_more_than_it(self):
        with self._configured(ENABLED=True, MAX_RAM_SHARE=4), self._machine():
            self.assertEqual(host_limits.ceilings().ram_bytes, self.RAM)

    def test_a_share_that_is_not_a_number_reads_as_no_ceiling(self):
        """Defensive only: `validate_host_policy_config` rejects one at load."""
        with self._configured(ENABLED=True, MAX_RAM_SHARE="half"), self._machine():
            self.assertIsNone(host_limits.ceilings())


class CommittedResourcesTests(HostLimitsTestCase):

    def _committed_from(self, rows):
        connection = mock.MagicMock()
        connection.get_committed_resources.return_value = rows
        module = types.ModuleType("src.database.sql_connection")
        module.SQLConnection = lambda: connection
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}):
            return host_limits.committed_resources()

    def test_the_grants_of_every_instance_are_added_up(self):
        held = self._committed_from([
            _row(mem=2 * GIB, disk=10 * GIB, quota=200_000),
            _row(mem=1 * GIB, disk=5 * GIB, quota=50_000),
        ])
        self.assertEqual(held.ram_bytes, 3 * GIB)
        self.assertEqual(held.disk_bytes, 15 * GIB)
        self.assertEqual(held.cores, 2.5)
        self.assertEqual(held.instances, 2)

    def test_a_row_with_empty_columns_contributes_nothing_rather_than_raising(self):
        held = self._committed_from([
            {"mem_limit": None, "disk_space": None, "cpu_quota": None, "cpu_period": None},
            _row(mem=GIB),
        ])
        self.assertEqual(held.ram_bytes, GIB)
        self.assertEqual(held.cores, 0.0)

    def test_a_quota_with_no_period_is_read_against_the_kernel_default(self):
        held = self._committed_from([_row(quota=150_000, period=0)])
        self.assertEqual(held.cores, 1.5)

    def test_an_unreadable_database_reports_nothing_held(self):
        """The direction that keeps a broken read from bricking the node."""
        module = types.ModuleType("src.database.sql_connection")

        def explode():
            raise RuntimeError("database is locked")

        module.SQLConnection = explode
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}), \
                mock.patch.object(host_limits, "logger") as logged:
            held = host_limits.committed_resources()
        self.assertEqual(held, host_limits.Committed(0.0, 0, 0, 0))
        logged.assert_called_once()


class BreachTests(HostLimitsTestCase):

    def _shortfalls(self, *, held, **request):
        with self._configured(ENABLED=True, MAX_CPU_SHARE=0.5, MAX_RAM_SHARE=0.5,
                              MAX_DISK_SHARE=0.5), self._machine():
            return host_limits.ceiling_shortfalls(committed=held, **request)

    EMPTY = None  # replaced in setUp, once the module is known to have imported

    def setUp(self):
        super().setUp()
        self.EMPTY = host_limits.Committed(cores=0.0, ram_bytes=0, disk_bytes=0, instances=0)

    def test_an_instance_that_fits_under_every_ceiling_is_admitted(self):
        self.assertEqual(
            self._shortfalls(held=self.EMPTY, cores=2, ram_bytes=4 * GIB, disk_bytes=100 * GIB),
            [],
        )

    def test_an_instance_is_measured_against_what_is_already_held(self):
        """Half of 16 GiB is 8; 6 already held leaves room for 2 and not for 3."""
        held = host_limits.Committed(cores=0.0, ram_bytes=6 * GIB, disk_bytes=0, instances=1)
        self.assertEqual(self._shortfalls(held=held, ram_bytes=2 * GIB), [])
        self.assertEqual(len(self._shortfalls(held=held, ram_bytes=3 * GIB)), 1)

    def test_every_ceiling_it_breaches_is_reported_not_just_the_first(self):
        shortfalls = self._shortfalls(
            held=self.EMPTY, cores=8, ram_bytes=12 * GIB, disk_bytes=400 * GIB,
        )
        self.assertEqual(len(shortfalls), 3, shortfalls)
        joined = " ".join(shortfalls)
        for key in ("MAX_CPU_SHARE", "MAX_RAM_SHARE", "MAX_DISK_SHARE"):
            self.assertIn(key, joined)

    def test_a_breach_names_the_key_the_operator_would_change(self):
        shortfall = self._shortfalls(held=self.EMPTY, ram_bytes=12 * GIB)[0]
        self.assertIn("host_limits.MAX_RAM_SHARE", shortfall)
        self.assertIn("GiB", shortfall)

    def test_nothing_is_refused_while_the_section_is_off(self):
        with self._configured(MAX_RAM_SHARE=0.5), self._machine():
            self.assertEqual(
                host_limits.ceiling_shortfalls(ram_bytes=1024 * GIB, committed=self.EMPTY),
                [],
            )

    def test_a_full_node_refuses_an_instance_that_declares_nothing(self):
        """Declaring no resources must not be the way past the ceiling.

        A service with no memory in its manifest still becomes an instance holding the
        virtualizer's floor, so a node already at its ceiling has to refuse it. Asking
        about a resource only when a positive figure came in would have made "ask for
        nothing" free, forever, however full the node was.
        """
        full = host_limits.Committed(
            cores=4.0, ram_bytes=8 * GIB, disk_bytes=250 * GIB, instances=9,
        )
        shortfalls = self._shortfalls(held=full, cores=0, ram_bytes=0, disk_bytes=0)
        self.assertEqual(shortfalls, [])
        over = host_limits.Committed(
            cores=5.0, ram_bytes=9 * GIB, disk_bytes=300 * GIB, instances=9,
        )
        self.assertEqual(len(self._shortfalls(held=over, cores=0, ram_bytes=0, disk_bytes=0)), 3)

    def test_a_resource_nobody_asked_about_is_not_judged(self):
        """None is what a resize passes for a resource it is leaving alone or shrinking.

        A node pushed over its ceiling -- the shares were lowered underneath it -- must
        not be stopped from releasing what would bring it back under, and the release
        arrives here as "nothing to ask about" for every other resource.
        """
        over = host_limits.Committed(
            cores=5.0, ram_bytes=9 * GIB, disk_bytes=300 * GIB, instances=9,
        )
        self.assertEqual(self._shortfalls(held=over), [])
        # Only the resource that grows is judged, even beside two that are over.
        shortfalls = self._shortfalls(held=over, ram_bytes=GIB)
        self.assertEqual(len(shortfalls), 1)
        self.assertIn("MAX_RAM_SHARE", shortfalls[0])


class DailyTrafficTests(HostLimitsTestCase):
    """The day's relayed volume, which has to outlive the process counting it."""

    def _counter(self, stored=0):
        connection = mock.MagicMock()
        connection.get_tunnel_traffic.return_value = stored
        module = types.ModuleType("src.database.sql_connection")
        module.SQLConnection = lambda: connection
        return host_limits._DailyTraffic(), connection, module

    def test_the_running_total_starts_from_what_the_database_already_holds(self):
        """A restart must not hand the operator a fresh allowance."""
        counter, connection, module = self._counter(stored=15 * GIB)
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}):
            self.assertEqual(counter.total(date(2026, 9, 4)), 15 * GIB)
            self.assertEqual(counter.account(GIB, date(2026, 9, 4)), 16 * GIB)
        connection.get_tunnel_traffic.assert_called_once_with(day="2026-09-04")

    def test_bytes_reach_the_database_in_blocks_rather_than_one_per_message(self):
        counter, connection, module = self._counter()
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}):
            for _ in range(3):
                counter.account(MIB, date(2026, 9, 4))
            connection.add_tunnel_traffic.assert_not_called()
            counter.account(host_limits.DAILY_FLUSH_BYTES, date(2026, 9, 4))
            connection.add_tunnel_traffic.assert_called_once()

    def test_settling_writes_out_what_did_not_fill_a_block(self):
        """Without this a run of short tunnels would each leave less than a block
        behind, and the allowance would never appear to be spent."""
        counter, connection, module = self._counter()
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}):
            counter.account(4096, date(2026, 9, 4))
            counter.flush()
        connection.add_tunnel_traffic.assert_called_once_with(day="2026-09-04", byte_count=4096)

    def test_midnight_starts_a_new_day_and_banks_the_old_one(self):
        counter, connection, module = self._counter()
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}):
            counter.account(4096, date(2026, 9, 4))
            connection.get_tunnel_traffic.return_value = 0
            self.assertEqual(counter.total(date(2026, 9, 5)), 0)
        connection.add_tunnel_traffic.assert_called_once_with(day="2026-09-04", byte_count=4096)

    def test_a_database_that_cannot_be_written_does_not_break_the_relay(self):
        counter, connection, module = self._counter()
        connection.add_tunnel_traffic.side_effect = RuntimeError("read-only")
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}), \
                mock.patch.object(host_limits, "logger"):
            counter.account(host_limits.DAILY_FLUSH_BYTES, date(2026, 9, 4))
            counter.flush()

    def test_no_cap_configured_means_the_counter_is_never_consulted(self):
        with self._configured(ENABLED=True, MAX_NET_GIB_PER_DAY=0):
            with mock.patch.object(host_limits, "_daily_traffic") as counter:
                self.assertTrue(host_limits.account_tunnel_traffic(GIB))
                self.assertFalse(host_limits.daily_allowance_spent())
                counter.account.assert_not_called()

    def test_traffic_is_refused_once_the_day_is_spent(self):
        with self._configured(ENABLED=True, MAX_NET_GIB_PER_DAY=1):
            with mock.patch.object(host_limits, "_daily_traffic") as counter:
                counter.account.return_value = GIB - 1
                self.assertTrue(host_limits.account_tunnel_traffic(4096))
                counter.account.return_value = GIB
                self.assertFalse(host_limits.account_tunnel_traffic(4096))
                counter.total.return_value = GIB
                self.assertTrue(host_limits.daily_allowance_spent())


class RateLimiterTests(HostLimitsTestCase):
    """Shaping, not closing: a transfer over the ceiling gets slower and still finishes."""

    def test_no_rate_configured_never_waits(self):
        limiter = host_limits._RateLimiter()
        with self._configured(ENABLED=True, MAX_NET_MIB_PER_SECOND=0):
            self.assertEqual(limiter.wait(64 * MIB), 0.0)

    def test_the_first_second_of_traffic_passes_without_waiting(self):
        """One second's worth of bucket, so a burst after an idle stretch is one second
        long rather than unbounded."""
        limiter = host_limits._RateLimiter()
        limiter._tokens = 5 * MIB
        with self._configured(ENABLED=True, MAX_NET_MIB_PER_SECOND=5), \
                mock.patch.object(host_limits.time, "sleep") as slept:
            self.assertEqual(limiter.wait(4 * MIB), 0.0)
            slept.assert_not_called()

    def test_traffic_over_the_ceiling_waits_for_the_shortfall(self):
        limiter = host_limits._RateLimiter()
        with self._configured(ENABLED=True, MAX_NET_MIB_PER_SECOND=4), \
                mock.patch.object(host_limits.time, "sleep") as slept:
            # An empty bucket and 2 MiB at 4 MiB/s is half a second.
            waited = limiter.wait(2 * MIB)
        self.assertAlmostEqual(waited, 0.5, places=2)
        slept.assert_called_once()

    def test_a_single_wait_is_bounded_and_the_debt_carries(self):
        """One 64 KiB read must not park a relay thread for a minute -- and the shaped
        rate has to come out the same anyway, so what could not be slept off stays in
        the bucket for the next call to pay."""
        limiter = host_limits._RateLimiter()
        with self._configured(ENABLED=True, MAX_NET_MIB_PER_SECOND=0.01), \
                mock.patch.object(host_limits.time, "sleep"):
            first = limiter.wait(MIB)
            self.assertEqual(first, host_limits.MAX_THROTTLE_WAIT_S)
            self.assertLess(limiter._tokens, 0)
            second = limiter.wait(1)
            self.assertGreater(second, 0)

    def test_the_bucket_is_shared_so_two_tunnels_do_not_each_get_the_ceiling(self):
        with self._configured(ENABLED=True, MAX_NET_MIB_PER_SECOND=4), \
                mock.patch.object(host_limits.time, "sleep"):
            host_limits._rate_limiter._tokens = 4 * MIB
            host_limits._rate_limiter._checked_at = host_limits.time.monotonic()
            self.assertEqual(host_limits.throttle_tunnel_traffic(4 * MIB), 0.0)
            # The second "tunnel" finds the bucket the first one emptied.
            self.assertGreater(host_limits.throttle_tunnel_traffic(4 * MIB), 0.0)


@unittest.skipIf(
    RESIZE_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {RESIZE_IMPORT_ERROR}",
)
class ResizeTests(unittest.TestCase):
    """A resize is judged on its growth, and only on the resources that grow.

    The subtle half is the shrink. Lower `MAX_RAM_SHARE` under a node that is already
    running and it is instantly over its own ceiling; the operation that fixes that is an
    instance releasing memory, so a resize check that refused it would leave the node
    stuck over the ceiling with no way down.
    """

    def test_only_the_resources_that_grow_are_asked_about(self):
        self.assertEqual(modify_resources._growth(10, 4), 6)
        self.assertIsNone(modify_resources._growth(4, 4))
        self.assertIsNone(modify_resources._growth(2, 4))


if __name__ == "__main__":
    unittest.main()
