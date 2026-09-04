"""A QEMU guest is booted with its ceiling and holds only its initial allocation.

``-m`` is fixed for the life of a QEMU process, so the headroom between a
manifest's ``at_init`` and its ``at_most`` has to be reserved when the guest boots
or it can never be grown into -- the balloon can only deflate back up to the boot
allocation, and the cgroup is a ceiling, not the knob. Reserving it is only half:
the guest must not *keep* the difference, which it was never granted and its
balance was not funded for, so the balloon takes it back as soon as the guest is
up. What the guest is left holding is what the instance is priced at, measured
rather than assumed.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.microvm import limits
    from src.virtualizers.qemu import hotplug as qemu_hotplug
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    limits = None  # type: ignore[assignment]
    qemu_hotplug = None  # type: ignore[assignment]

MIB = 1024 * 1024


def _resources(at_init_mem=None, at_most_mem=None):
    resources = celaut.Service.Container.Resources()
    if at_init_mem is not None:
        resources.at_init.mem_limit = at_init_mem
    if at_most_mem is not None:
        resources.at_most.mem_limit = at_most_mem
    return resources


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BootMemoryReservationTests(unittest.TestCase):
    """What `-m` has to be for the declared ceiling to be reachable later."""

    def test_the_ceiling_is_what_the_guest_is_booted_with(self):
        self.assertEqual(
            limits.resolve_boot_mem_bytes(_resources(256 * MIB, 2048 * MIB)), 2048 * MIB
        )

    def test_the_initial_allocation_is_not_what_bounds_a_later_grow(self):
        # The bug this exists for: booting at at_init caps every later resize at
        # at_init, so the declared headroom is unreachable for the guest's whole
        # life however the hotplug is called.
        boot = limits.resolve_boot_mem_bytes(_resources(256 * MIB, 2048 * MIB))
        self.assertGreater(boot, 256 * MIB)

    def test_a_manifest_with_no_headroom_reserves_nothing_extra(self):
        self.assertEqual(limits.resolve_boot_mem_bytes(_resources(512 * MIB)), 512 * MIB)

    def test_a_ceiling_below_the_initial_allocation_is_not_a_shrink(self):
        # Malformed, but it must not boot the guest with less than it was granted.
        self.assertEqual(
            limits.resolve_boot_mem_bytes(_resources(512 * MIB, 128 * MIB)), 512 * MIB
        )

    def test_the_boot_floor_still_applies(self):
        # Same floor `resolve_initial_resources` enforces: below it the kernel plus
        # initramfs never reaches console.
        self.assertEqual(
            limits.resolve_boot_mem_bytes(_resources(1 * MIB)), limits.MIN_MEM_MIB * MIB
        )

    def test_an_empty_manifest_gets_the_default(self):
        self.assertEqual(
            limits.resolve_boot_mem_bytes(_resources()), limits.DEFAULT_MEM_MIB * MIB
        )
        self.assertEqual(
            limits.resolve_boot_mem_bytes(None), limits.DEFAULT_MEM_MIB * MIB
        )

    def test_a_ceiling_alone_is_still_honoured(self):
        self.assertEqual(
            limits.resolve_boot_mem_bytes(_resources(at_most_mem=4096 * MIB)), 4096 * MIB
        )


class _FakeGuest:
    """A guest that meets a balloon target as far as its own usage allows.

    Stands in for `QMPClient`: the boot squeeze only ever reaches the guest
    through `set_balloon` / `balloon_actual_bytes` / `guest_free_bytes`.
    """

    def __init__(self, boot_bytes, in_use_bytes, honours=True, reports_stats=True):
        self.boot = boot_bytes
        self.in_use = in_use_bytes
        self.actual = boot_bytes
        self.honours = honours
        self.reports_stats = reports_stats
        self.targets = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_balloon(self, target_bytes):
        self.targets.append(int(target_bytes))
        if self.honours:
            # The guest surrenders free pages only; it cannot go below what it uses.
            self.actual = max(int(target_bytes), self.in_use)

    def balloon_actual_bytes(self):
        return self.actual

    def guest_free_bytes(self):
        return max(0, self.actual - self.in_use) if self.reports_stats else None


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SettleBootBalloonTests(unittest.TestCase):
    def _settle(self, guest, boot_bytes, target_bytes, timeout_s=5.0):
        with patch.object(qemu_hotplug, "QMPClient", return_value=guest), \
             patch.object(qemu_hotplug, "BOOT_BALLOON_POLL_INTERVAL_S", 0):
            return qemu_hotplug.settle_boot_balloon(
                vmachine_id="vm-boot",
                qmp_socket="/run/qmp.sock",
                boot_mem_bytes=boot_bytes,
                target_bytes=target_bytes,
                timeout_s=timeout_s,
            )

    def test_the_headroom_is_handed_back_and_the_guest_holds_at_init(self):
        guest = _FakeGuest(boot_bytes=2048 * MIB, in_use_bytes=90 * MIB)
        held = self._settle(guest, boot_bytes=2048 * MIB, target_bytes=256 * MIB)
        self.assertEqual(held, 256 * MIB)
        self.assertEqual(guest.targets[-1], 256 * MIB)

    def test_a_guest_with_no_headroom_is_never_asked_for_anything(self):
        # at_most == at_init: the guest was booted with what it was granted, so
        # there is nothing to reclaim and no reason to open a QMP connection.
        guest = _FakeGuest(boot_bytes=512 * MIB, in_use_bytes=90 * MIB)
        held = self._settle(guest, boot_bytes=512 * MIB, target_bytes=512 * MIB)
        self.assertEqual(held, 512 * MIB)
        self.assertEqual(guest.targets, [])

    def test_a_guest_that_keeps_the_headroom_is_billed_for_it(self):
        # No balloon driver: the target is recorded and never fulfilled. Pricing
        # this at at_init would bill a guest for less memory than it holds.
        guest = _FakeGuest(boot_bytes=2048 * MIB, in_use_bytes=90 * MIB, honours=False)
        held = self._settle(guest, boot_bytes=2048 * MIB, target_bytes=256 * MIB, timeout_s=0.0)
        self.assertEqual(held, 2048 * MIB)

    def test_a_guest_that_cannot_report_keeps_what_it_has(self):
        # Same rule the hotplug shrink follows: unknown usage means no safe amount
        # to reclaim, so the guest keeps its allocation -- and pays for it.
        guest = _FakeGuest(
            boot_bytes=2048 * MIB, in_use_bytes=90 * MIB, reports_stats=False
        )
        held = self._settle(guest, boot_bytes=2048 * MIB, target_bytes=256 * MIB)
        self.assertEqual(held, 2048 * MIB)

    def test_the_squeeze_never_takes_a_busy_guest_below_what_it_uses(self):
        # A guest already using more than its at_init by the time it is up. The
        # boot squeeze is a shrink like any other and goes through the same clamp,
        # so it reclaims what it can rather than OOM-panicking the guest.
        guest = _FakeGuest(boot_bytes=2048 * MIB, in_use_bytes=600 * MIB)
        held = self._settle(guest, boot_bytes=2048 * MIB, target_bytes=256 * MIB)
        self.assertGreaterEqual(held, 600 * MIB)
        self.assertLess(held, 2048 * MIB)
        self.assertTrue(all(t >= 600 * MIB for t in guest.targets), guest.targets)

    def test_a_qmp_failure_leaves_the_guest_holding_its_boot_allocation(self):
        class _Unreachable:
            def __enter__(self_):
                raise OSError("no such socket")

            def __exit__(self_, *exc):
                return False

        held = self._settle(_Unreachable(), boot_bytes=2048 * MIB, target_bytes=256 * MIB)
        self.assertEqual(held, 2048 * MIB)

    def test_the_target_is_never_taken_below_the_absolute_floor(self):
        guest = _FakeGuest(boot_bytes=2048 * MIB, in_use_bytes=1 * MIB)
        held = self._settle(guest, boot_bytes=2048 * MIB, target_bytes=0)
        self.assertGreaterEqual(held, qemu_hotplug.MIN_BALLOON_BYTES)


if __name__ == "__main__":
    unittest.main()
