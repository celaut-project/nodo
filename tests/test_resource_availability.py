"""Unit tests for the local admission gate (src/utils/cost_functions/resource_availability.py).

Every limit a Sysresources can declare is checked, not just `mem_limit`, and the three
kinds are checked against different things on purpose -- memory and disk against what is
free right now, a CPU quota against the host's core count, blkio_weight against the range
cgroups accept. These tests pin each of those, and pin the *reason* strings too: they are
what a peer receives over GetResourceAvailability and what an operator reads in a refusal.

The module deliberately depends on nothing but psutil and the memory pool, so this file
needs no stubbing at all.
"""
import unittest
from unittest.mock import patch

from protos import celaut_pb2 as celaut
from src.utils.cost_functions import resource_availability as ra


def _resources(**fields) -> celaut.Service.Container.Resources:
    return celaut.Service.Container.Resources(at_most=celaut.Sysresources(**fields))


def _start(**fields) -> celaut.Service.Container.Resources:
    return celaut.Service.Container.Resources(at_init=celaut.Sysresources(**fields))


class RequestedCoresTests(unittest.TestCase):
    def test_quota_over_period_is_cores(self):
        self.assertEqual(ra._requested_cores(celaut.Sysresources(cpu_quota=400000, cpu_period=100000)), 4.0)

    def test_an_omitted_period_uses_the_kernel_default(self):
        # 200000us of quota is two cores under the default 100000us CFS period.
        self.assertEqual(ra._requested_cores(celaut.Sysresources(cpu_quota=200000)), 2.0)

    def test_no_quota_is_no_request(self):
        self.assertEqual(ra._requested_cores(celaut.Sysresources(cpu_period=100000)), 0.0)


class SysreqShortfallTests(unittest.TestCase):
    """`could_ve_this_sysreq` is patched so only the limit under test can fail.

    The two ends are held against different things: ``at_init`` (what the node hands
    over at the start) against what is free now, ``at_most`` (where it may grow) against
    what the machine has in total.
    """

    def _shortfalls(self, at_init=None, at_most=None, *, memory_fits=True, disk_free=10**12,
                    disk_total=10**13, cpu_total=8, ram_total=10**12):
        with patch.object(ra, "could_ve_this_sysreq", return_value=memory_fits):
            return ra._sysreq_shortfalls(
                at_init or celaut.Sysresources(), at_most or celaut.Sysresources(),
                disk_free=disk_free, disk_total=disk_total, cpu_total=cpu_total,
                ram_total=ram_total, pool_total=1, pool_available=1,
            )

    def test_a_declaration_this_host_can_honour_has_no_shortfalls(self):
        sysreq = celaut.Sysresources(
            mem_limit=1024, disk_space=1024, cpu_quota=100000, cpu_period=100000, blkio_weight=500
        )
        self.assertEqual(self._shortfalls(sysreq, sysreq), [])

    def test_the_start_memory_is_asked_of_the_pool(self):
        found = self._shortfalls(celaut.Sysresources(mem_limit=10**15), memory_fits=False)
        self.assertEqual(len(found), 1)
        self.assertIn("at_init.mem_limit", found[0])

    def test_the_maximum_memory_is_not_asked_of_the_pool(self):
        # What is free now is the start's question. A maximum the machine could hold
        # when it is emptier is not refused because the pool is short today: the pool is
        # asked about the start's memory only.
        asked = []
        with patch.object(ra, "could_ve_this_sysreq",
                          side_effect=lambda sysreq: asked.append(int(sysreq.mem_limit)) or True):
            found = ra._sysreq_shortfalls(
                celaut.Sysresources(mem_limit=5), celaut.Sysresources(mem_limit=10**9),
                disk_free=10**12, disk_total=10**13, cpu_total=8, ram_total=10**10,
                pool_total=1, pool_available=1,
            )
        self.assertEqual(found, [])
        self.assertEqual(asked, [5])

    def test_a_maximum_beyond_the_machine_s_total_memory_is_reported(self):
        found = self._shortfalls(at_most=celaut.Sysresources(mem_limit=2048), ram_total=1024)
        self.assertEqual(len(found), 1)
        self.assertIn("at_most.mem_limit", found[0])

    def test_a_maximum_exactly_the_total_memory_fits(self):
        self.assertEqual(
            self._shortfalls(at_most=celaut.Sysresources(mem_limit=1024), ram_total=1024), [])

    def test_an_unknown_total_memory_skips_the_maximum_check(self):
        self.assertEqual(
            self._shortfalls(at_most=celaut.Sysresources(mem_limit=10**15), ram_total=0), [])

    def test_start_disk_beyond_what_is_free_is_reported(self):
        found = self._shortfalls(celaut.Sysresources(disk_space=2048), disk_free=1024)
        self.assertEqual(len(found), 1)
        self.assertIn("at_init.disk_space", found[0])

    def test_start_disk_exactly_equal_to_what_is_free_fits(self):
        self.assertEqual(self._shortfalls(celaut.Sysresources(disk_space=1024), disk_free=1024), [])

    def test_maximum_disk_is_not_held_against_what_is_free(self):
        self.assertEqual(
            self._shortfalls(at_most=celaut.Sysresources(disk_space=2048), disk_free=1024,
                             disk_total=4096),
            [],
        )

    def test_a_maximum_disk_beyond_the_total_is_reported(self):
        found = self._shortfalls(at_most=celaut.Sysresources(disk_space=2048), disk_total=1024)
        self.assertEqual(len(found), 1)
        self.assertIn("at_most.disk_space", found[0])

    def test_a_maximum_disk_beyond_the_total_is_not_cured_by_freeing_disk(self):
        # `short_of_disk_only` makes a launch delete suspended instances for room; that
        # is pointless for a disk that is too small however empty it is.
        found = self._shortfalls(at_most=celaut.Sysresources(disk_space=2048), disk_total=1024)
        self.assertFalse(ra.short_of_disk_only({"can_execute": False, "reason": " | ".join(found)}))

    def test_a_start_disk_over_what_is_free_is_cured_by_freeing_disk(self):
        found = self._shortfalls(celaut.Sysresources(disk_space=2048), disk_free=1024)
        self.assertTrue(ra.short_of_disk_only({"can_execute": False, "reason": " | ".join(found)}))

    def test_a_quota_larger_than_the_host_has_cores_is_reported(self):
        for end in ("at_init", "at_most"):
            with self.subTest(end=end):
                sysreq = celaut.Sysresources(cpu_quota=900000)
                found = self._shortfalls(**{end: sysreq}, cpu_total=8)
                self.assertEqual(len(found), 1)
                self.assertIn(f"{end}.cpu_quota", found[0])

    def test_a_quota_the_host_can_serve_fits(self):
        self.assertEqual(self._shortfalls(at_most=celaut.Sysresources(cpu_quota=800000), cpu_total=8), [])

    def test_cpu_is_not_judged_on_instantaneous_load(self):
        # A quota is a share of time, so a busy host is not a full one. Nothing in the
        # shortfall path reads CPU utilisation -- only the core count.
        with patch("psutil.cpu_percent", return_value=100.0):
            self.assertEqual(self._shortfalls(at_most=celaut.Sysresources(cpu_quota=100000), cpu_total=8), [])

    def test_an_unknown_core_count_skips_the_cpu_check(self):
        # psutil returns None for physical cores on some platforms; an unknown capacity
        # is not evidence of an insufficient one.
        self.assertEqual(self._shortfalls(at_most=celaut.Sysresources(cpu_quota=10**9), cpu_total=0), [])

    def test_blkio_weight_outside_the_cgroup_range_is_reported(self):
        for weight in (5, 1001):
            with self.subTest(weight=weight):
                found = self._shortfalls(at_most=celaut.Sysresources(blkio_weight=weight))
                self.assertEqual(len(found), 1)
                self.assertIn("blkio_weight", found[0])

    def test_blkio_weight_at_the_range_edges_is_accepted(self):
        for weight in (10, 1000):
            with self.subTest(weight=weight):
                self.assertEqual(self._shortfalls(at_most=celaut.Sysresources(blkio_weight=weight)), [])

    def test_every_shortfall_is_reported_not_only_the_first(self):
        # A service asking for more memory *and* more disk than exists should be told
        # both, or it fixes one and comes back for the other.
        at_init = celaut.Sysresources(mem_limit=10**15, disk_space=2048)
        at_most = celaut.Sysresources(cpu_quota=900000)
        found = self._shortfalls(at_init, at_most, memory_fits=False, disk_free=1024, cpu_total=8)
        self.assertEqual(len(found), 3)

    def test_the_ceiling_adds_the_start_to_what_is_held_and_holds_the_maximum_alone(self):
        at_init = celaut.Sysresources(mem_limit=1, disk_space=2, cpu_quota=100000)
        at_most = celaut.Sysresources(mem_limit=10, disk_space=20, cpu_quota=400000)
        with patch.object(ra.host_limits, "ceiling_shortfalls", return_value=[]) as start, \
                patch.object(ra.host_limits, "maximum_shortfalls", return_value=[]) as most:
            self._shortfalls(at_init, at_most)
        start.assert_called_once_with(cores=1.0, ram_bytes=1, disk_bytes=2)
        most.assert_called_once_with(cores=4.0, ram_bytes=10, disk_bytes=20)


class GetResourceAvailabilityTests(unittest.TestCase):
    def test_no_resources_declared_can_execute(self):
        self.assertTrue(ra.get_resource_availability(celaut.Service.Container.Resources())["can_execute"])

    def test_an_unsatisfiable_declaration_carries_its_reason(self):
        with patch.object(ra, "_sysreq_shortfalls", return_value=["first", "second"]):
            answer = ra.get_resource_availability(_resources(mem_limit=1))
        self.assertFalse(answer["can_execute"])
        self.assertEqual(answer["reason"], "first | second")

    def test_a_satisfiable_declaration_has_an_empty_reason(self):
        with patch.object(ra, "_sysreq_shortfalls", return_value=[]):
            answer = ra.get_resource_availability(_resources(mem_limit=1))
        self.assertTrue(answer["can_execute"])
        self.assertEqual(answer["reason"], "")

    def test_a_declaration_with_only_a_start_is_still_checked(self):
        with patch.object(ra, "_sysreq_shortfalls", return_value=["no room"]) as checked:
            answer = ra.get_resource_availability(_start(mem_limit=1))
        self.assertFalse(answer["can_execute"])
        checked.assert_called_once()
        self.assertEqual(answer["requested_mem_limit"], 1)

    def test_the_two_ends_reach_the_check_each_in_its_own_place(self):
        resources = celaut.Service.Container.Resources(
            at_init=celaut.Sysresources(mem_limit=1), at_most=celaut.Sysresources(mem_limit=9))
        with patch.object(ra, "_sysreq_shortfalls", return_value=[]) as checked:
            ra.get_resource_availability(resources)
        at_init, at_most = checked.call_args.args
        self.assertEqual((at_init.mem_limit, at_most.mem_limit), (1, 9))

    def test_the_telemetry_keys_the_wire_answer_is_built_from_are_present(self):
        answer = ra.get_resource_availability(_resources(mem_limit=1))
        for key in ("can_execute", "reason", "requested_mem_limit",
                    "service_memory_pool_total", "service_memory_pool_available"):
            self.assertIn(key, answer)


if __name__ == "__main__":
    unittest.main()
