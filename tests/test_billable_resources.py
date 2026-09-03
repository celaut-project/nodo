"""The figures a quote is computed from must be the figures the tick charges.

Every case here is a manifest that asks for less than the guest is actually given, which
is where the two can part company: what `limits.billable_resources` returns is what the
`local_instances` row will hold, so it is also what a quote has to say.
"""
import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.ch import limits
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    limits = None  # type: ignore[assignment]

MIB = 1024 * 1024


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BillableResourcesTests(unittest.TestCase):

    def test_a_manifest_that_declares_no_cpu_is_billed_the_vcpu_it_gets(self):
        # Without a CFS pair `requested_units` reads zero vCPUs, so this resolution is
        # all that keeps the CPU line of a quote from reading free for a guest that runs
        # on one vCPU.
        billable = limits.billable_resources(celaut.Sysresources(mem_limit=512 * MIB))
        self.assertEqual(billable.cpu_period, 100000)
        self.assertEqual(billable.cpu_quota, 100000)

    def test_declared_cpu_is_left_alone(self):
        billable = limits.billable_resources(
            celaut.Sysresources(cpu_period=100000, cpu_quota=400000)
        )
        self.assertEqual(billable.cpu_period, 100000)
        self.assertEqual(billable.cpu_quota, 400000)

    def test_ram_under_the_boot_floor_is_billed_at_the_floor(self):
        billable = limits.billable_resources(celaut.Sysresources(mem_limit=50 * 10 ** 6))
        self.assertEqual(billable.mem_limit, limits.MIN_MEM_MIB * MIB)

    def test_ram_over_the_floor_is_billed_as_declared(self):
        billable = limits.billable_resources(celaut.Sysresources(mem_limit=8 * 1024 * MIB))
        self.assertEqual(billable.mem_limit, 8 * 1024 * MIB)

    def test_undeclared_ram_is_billed_at_the_default_the_guest_gets(self):
        billable = limits.billable_resources(celaut.Sysresources())
        self.assertEqual(billable.mem_limit, limits.DEFAULT_MEM_MIB * MIB)

    def test_the_guest_kernel_reserve_is_not_billed_to_the_client(self):
        """The VM is booted larger than this figure; the client is not charged for it.

        The extra RAM exists so the service can allocate what it declared -- it is the
        node's cost of delivering the promise, not something the client asked for.
        """
        declared = 256 * MIB
        billable = limits.billable_resources(celaut.Sysresources(mem_limit=declared))
        self.assertEqual(billable.mem_limit, declared)
        self.assertGreater(limits.guest_boot_memory_bytes(declared), billable.mem_limit)

    def test_disk_under_the_image_floor_is_billed_at_the_floor(self):
        billable = limits.billable_resources(celaut.Sysresources(disk_space=10 * MIB))
        self.assertEqual(billable.disk_space, limits.MIN_ROOTFS_BYTES)

    def test_a_built_service_is_billed_the_image_its_instances_receive(self):
        # The floor is only a lower bound: the populated tree plus overhead, and every
        # mkfs growth retry, are known only once the image exists. When the caller can
        # read that image, the quote stops guessing.
        built = 3 * 1024 * MIB
        billable = limits.billable_resources(
            celaut.Sysresources(disk_space=2 * 10 ** 9),
            built_rootfs_size_bytes=built,
        )
        self.assertEqual(billable.disk_space, built)

    def test_a_declaration_above_the_built_image_is_billed_as_declared(self):
        # The build floors the image at the declared figure, so a declaration larger
        # than a stale bundle's image is what the next instance will hold.
        billable = limits.billable_resources(
            celaut.Sysresources(disk_space=4 * 1024 * MIB),
            built_rootfs_size_bytes=1024 * MIB,
        )
        self.assertEqual(billable.disk_space, 4 * 1024 * MIB)

    def test_resolving_an_already_resolved_row_changes_nothing(self):
        # The maintenance tick prices `local_instances`, whose values come from the
        # virtualizer and are already floored. Quoting and charging run the same
        # resolution over the same numbers, so they must agree; were that to stop
        # holding, a client would be quoted one figure and billed another.
        declared = celaut.Sysresources(mem_limit=50 * 10 ** 6, disk_space=10 * MIB)
        once = limits.billable_resources(declared)
        twice = limits.billable_resources(once)
        self.assertEqual(once, twice)

    def test_none_resources_resolve_to_the_defaults_rather_than_raising(self):
        # `default_initial_balance` short-circuits on None, but nothing stops another
        # caller from handing this an empty scope; the floors are the safe answer.
        billable = limits.billable_resources(None)
        self.assertEqual(billable.mem_limit, limits.DEFAULT_MEM_MIB * MIB)
        self.assertEqual(billable.disk_space, limits.MIN_ROOTFS_BYTES)
        self.assertEqual(billable.cpu_quota, 100000)


if __name__ == "__main__":
    unittest.main()
