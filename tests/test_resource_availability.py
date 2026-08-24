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


class RequestedCoresTests(unittest.TestCase):
    def test_quota_over_period_is_cores(self):
        self.assertEqual(ra._requested_cores(celaut.Sysresources(cpu_quota=400000, cpu_period=100000)), 4.0)

    def test_an_omitted_period_uses_the_kernel_default(self):
        # 200000us of quota is two cores under the default 100000us CFS period.
        self.assertEqual(ra._requested_cores(celaut.Sysresources(cpu_quota=200000)), 2.0)

    def test_no_quota_is_no_request(self):
        self.assertEqual(ra._requested_cores(celaut.Sysresources(cpu_period=100000)), 0.0)


class SysreqShortfallTests(unittest.TestCase):
    """`could_ve_this_sysreq` is patched so only the limit under test can fail."""

    def _shortfalls(self, sysreq, *, memory_fits=True, disk_free=10**12, cpu_total=8):
        with patch.object(ra, "could_ve_this_sysreq", return_value=memory_fits):
            return ra._sysreq_shortfalls(
                sysreq, disk_free=disk_free, cpu_total=cpu_total, pool_total=1, pool_available=1
            )

    def test_a_declaration_this_host_can_honour_has_no_shortfalls(self):
        sysreq = celaut.Sysresources(
            mem_limit=1024, disk_space=1024, cpu_quota=100000, cpu_period=100000, blkio_weight=500
        )
        self.assertEqual(self._shortfalls(sysreq), [])

    def test_memory_is_reported_when_the_pool_cannot_take_it(self):
        found = self._shortfalls(celaut.Sysresources(mem_limit=10**15), memory_fits=False)
        self.assertEqual(len(found), 1)
        self.assertIn("mem_limit", found[0])

    def test_disk_beyond_what_is_free_is_reported(self):
        found = self._shortfalls(celaut.Sysresources(disk_space=2048), disk_free=1024)
        self.assertEqual(len(found), 1)
        self.assertIn("disk_space", found[0])

    def test_disk_exactly_equal_to_what_is_free_fits(self):
        self.assertEqual(self._shortfalls(celaut.Sysresources(disk_space=1024), disk_free=1024), [])

    def test_a_quota_larger_than_the_host_has_cores_is_reported(self):
        found = self._shortfalls(celaut.Sysresources(cpu_quota=900000), cpu_total=8)
        self.assertEqual(len(found), 1)
        self.assertIn("cpu_quota", found[0])

    def test_a_quota_the_host_can_serve_fits(self):
        self.assertEqual(self._shortfalls(celaut.Sysresources(cpu_quota=800000), cpu_total=8), [])

    def test_cpu_is_not_judged_on_instantaneous_load(self):
        # A quota is a share of time, so a busy host is not a full one. Nothing in the
        # shortfall path reads CPU utilisation -- only the core count.
        with patch("psutil.cpu_percent", return_value=100.0):
            self.assertEqual(self._shortfalls(celaut.Sysresources(cpu_quota=100000), cpu_total=8), [])

    def test_an_unknown_core_count_skips_the_cpu_check(self):
        # psutil returns None for physical cores on some platforms; an unknown capacity
        # is not evidence of an insufficient one.
        self.assertEqual(self._shortfalls(celaut.Sysresources(cpu_quota=10**9), cpu_total=0), [])

    def test_blkio_weight_outside_the_cgroup_range_is_reported(self):
        for weight in (5, 1001):
            with self.subTest(weight=weight):
                found = self._shortfalls(celaut.Sysresources(blkio_weight=weight))
                self.assertEqual(len(found), 1)
                self.assertIn("blkio_weight", found[0])

    def test_blkio_weight_at_the_range_edges_is_accepted(self):
        for weight in (10, 1000):
            with self.subTest(weight=weight):
                self.assertEqual(self._shortfalls(celaut.Sysresources(blkio_weight=weight)), [])

    def test_every_shortfall_is_reported_not_only_the_first(self):
        # A service asking for more memory *and* more disk than exists should be told
        # both, or it fixes one and comes back for the other.
        sysreq = celaut.Sysresources(mem_limit=10**15, disk_space=2048, cpu_quota=900000)
        found = self._shortfalls(sysreq, memory_fits=False, disk_free=1024, cpu_total=8)
        self.assertEqual(len(found), 3)


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

    def test_the_telemetry_keys_the_wire_answer_is_built_from_are_present(self):
        answer = ra.get_resource_availability(_resources(mem_limit=1))
        for key in ("can_execute", "reason", "requested_mem_limit",
                    "service_memory_pool_total", "service_memory_pool_available"):
            self.assertIn(key, answer)


if __name__ == "__main__":
    unittest.main()
