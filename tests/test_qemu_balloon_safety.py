"""A memory shrink must never OOM-panic the guest it is resizing.

The balloon's usual safety argument -- "the guest only surrenders free pages" --
is about which pages move, not about which *target* is legal. Ask a guest to
shrink below what it is actually using and the driver keeps allocating to satisfy
the request until the guest allocator gives up; the guest kernel then panics with
"Out of memory and no killable processes".

Reproduced on a live arm64-on-x86 guest: a caller requested 64 MiB against a
954 MiB boot allocation, and the guest died mid-resize with the endpoint already
published. So the shrink is bounded by what the guest reports it can spare.
"""
import unittest
import unittest.mock
from unittest.mock import MagicMock

IMPORT_ERROR = None
try:
    from src.virtualizers.qemu import hotplug as qemu_hotplug
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    qemu_hotplug = None  # type: ignore[assignment]

MIB = 1024 * 1024


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SafeBalloonTargetTests(unittest.TestCase):
    def _qmp(self, free_bytes, actual_bytes=None):
        """A QMP client reporting `free_bytes` free of `actual_bytes` it holds.

        `actual_bytes=None` is a guest whose current allocation QEMU cannot
        report, which must fall back to the boot allocation.
        """
        qmp = MagicMock()
        qmp.guest_free_bytes.return_value = free_bytes
        qmp.balloon_actual_bytes.return_value = actual_bytes
        return qmp

    def test_the_reproducer_is_clamped_instead_of_killing_the_guest(self):
        # 954 MiB boot, guest has 100 MiB free => ~854 MiB in use. A 64 MiB
        # request cannot be honoured; it must not be attempted either.
        target, note = qemu_hotplug._safe_balloon_target(
            self._qmp(100 * MIB), requested=64 * MIB, boot_mem_bytes=954 * MIB
        )
        self.assertGreater(target, 64 * MIB)
        self.assertEqual(target, (954 - 100) * MIB + qemu_hotplug.BALLOON_SAFETY_MARGIN_BYTES)
        self.assertIn("clamped", note)

    def test_a_request_the_guest_can_afford_is_untouched(self):
        # 900 MiB free of 1024 MiB => only ~124 MiB in use; 512 MiB is safe.
        target, note = qemu_hotplug._safe_balloon_target(
            self._qmp(900 * MIB), requested=512 * MIB, boot_mem_bytes=1024 * MIB
        )
        self.assertEqual(target, 512 * MIB)
        self.assertIsNone(note)

    def test_a_grow_is_never_clamped_by_the_safety_floor(self):
        target, note = qemu_hotplug._safe_balloon_target(
            self._qmp(10 * MIB), requested=1024 * MIB, boot_mem_bytes=1024 * MIB
        )
        self.assertEqual(target, 1024 * MIB)
        self.assertIsNone(note)

    def test_the_bound_is_measured_against_what_the_guest_has_not_its_boot_alloc(self):
        # Already inflated: 954 MiB boot, but the guest only holds 600 MiB, of
        # which 100 MiB is free => 500 MiB in use. Measuring against the boot
        # allocation instead would count the 354 MiB the balloon already holds as
        # "in use" and compute a floor of 918 MiB -- above what the guest has.
        target, note = qemu_hotplug._safe_balloon_target(
            self._qmp(100 * MIB, actual_bytes=600 * MIB),
            requested=500 * MIB,
            boot_mem_bytes=954 * MIB,
        )
        self.assertEqual(target, 500 * MIB + qemu_hotplug.BALLOON_SAFETY_MARGIN_BYTES)
        self.assertIn("clamped", note)

    def test_a_clamp_never_raises_the_target_above_what_the_guest_has(self):
        # A shrink request against a guest with almost nothing free: the floor
        # would land above the current allocation, which would *deflate* the
        # balloon -- a shrink request must never hand the guest more memory.
        target, _ = qemu_hotplug._safe_balloon_target(
            self._qmp(1 * MIB, actual_bytes=600 * MIB),
            requested=500 * MIB,
            boot_mem_bytes=954 * MIB,
        )
        self.assertLessEqual(target, 600 * MIB)

    def test_a_partial_grow_is_honoured_rather_than_clamped(self):
        # Requesting more than the guest currently holds is a grow; the safety
        # floor has no business touching it, however little the guest has free.
        target, note = qemu_hotplug._safe_balloon_target(
            self._qmp(1 * MIB, actual_bytes=600 * MIB),
            requested=700 * MIB,
            boot_mem_bytes=954 * MIB,
        )
        self.assertEqual(target, 700 * MIB)
        self.assertIsNone(note)

    def test_a_guest_that_cannot_report_is_not_shrunk_at_all(self):
        # Unknown usage means no safe amount to reclaim. A guest that cannot
        # publish statistics is typically one with no balloon driver, which would
        # not return the pages anyway -- so leave it what it has.
        target, note = qemu_hotplug._safe_balloon_target(
            self._qmp(None), requested=64 * MIB, boot_mem_bytes=954 * MIB
        )
        self.assertEqual(target, 954 * MIB)
        self.assertIn("does not report", note)

    def test_a_blind_guest_is_held_at_what_it_has_not_at_its_boot_alloc(self):
        target, _ = qemu_hotplug._safe_balloon_target(
            self._qmp(None, actual_bytes=600 * MIB),
            requested=64 * MIB,
            boot_mem_bytes=954 * MIB,
        )
        self.assertEqual(target, 600 * MIB)

    def test_the_blind_target_never_exceeds_the_boot_allocation(self):
        target, _ = qemu_hotplug._safe_balloon_target(
            self._qmp(None), requested=64 * MIB, boot_mem_bytes=128 * MIB
        )
        self.assertLessEqual(target, 128 * MIB)

    def test_a_blind_guest_still_honours_a_grow(self):
        target, note = qemu_hotplug._safe_balloon_target(
            self._qmp(None, actual_bytes=512 * MIB),
            requested=700 * MIB,
            boot_mem_bytes=954 * MIB,
        )
        self.assertEqual(target, 700 * MIB)
        self.assertIsNone(note)

    def test_a_client_that_cannot_be_asked_is_not_an_error(self):
        # A QMP client from before these readings existed has no such methods.
        # That is "cannot tell", not a failed resize -- it must not raise.
        class _OldClient:
            def set_balloon(self, target_bytes):
                pass

        target, note = qemu_hotplug._safe_balloon_target(
            _OldClient(), requested=64 * MIB, boot_mem_bytes=954 * MIB
        )
        self.assertEqual(target, 954 * MIB)
        self.assertIn("does not report", note)

    def test_the_request_is_never_taken_below_the_absolute_floor(self):
        target, _ = qemu_hotplug._safe_balloon_target(
            self._qmp(1024 * MIB), requested=0, boot_mem_bytes=1024 * MIB
        )
        self.assertGreaterEqual(target, qemu_hotplug.MIN_BALLOON_BYTES)

    def test_a_fully_free_guest_can_be_shrunk_close_to_the_floor(self):
        # Nothing in use => the only bound left is the margin and the floor.
        target, _ = qemu_hotplug._safe_balloon_target(
            self._qmp(1024 * MIB), requested=64 * MIB, boot_mem_bytes=1024 * MIB
        )
        self.assertEqual(target, 64 * MIB)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BalloonStatsReadingTests(unittest.TestCase):
    """`guest_free_bytes` must distinguish "no data" from "no free memory"."""

    def _client(self, return_value=None, raises=None):
        from src.virtualizers.qemu.qmp import QMPClient

        client = QMPClient.__new__(QMPClient)
        if raises is not None:
            client._execute = MagicMock(side_effect=raises)
        else:
            client._execute = MagicMock(return_value=return_value)
        return client

    def test_reports_free_memory_from_guest_stats(self):
        client = self._client({"stats": {"stat-free-memory": 123 * MIB}})
        self.assertEqual(client.guest_free_bytes(), 123 * MIB)

    def test_a_guest_that_has_not_reported_yet_answers_unknown_not_zero(self):
        # All-zero stats mean the guest has not published yet. Treating that as
        # "zero free" would justify the exact inflation that kills it.
        client = self._client({"stats": {"stat-free-memory": 0}})
        self.assertIsNone(client.guest_free_bytes())

    def test_a_qmp_error_is_unknown_rather_than_fatal(self):
        from src.virtualizers.qemu.qmp import QMPError

        client = self._client(raises=QMPError("no such property"))
        self.assertIsNone(client.guest_free_bytes())

    def test_missing_or_malformed_stats_are_unknown(self):
        self.assertIsNone(self._client({}).guest_free_bytes())
        self.assertIsNone(self._client({"stats": {}}).guest_free_bytes())
        self.assertIsNone(
            self._client({"stats": {"stat-free-memory": "not-a-number"}}).guest_free_bytes()
        )

    def test_reports_the_memory_the_guest_currently_holds(self):
        # `actual` is boot -m less the inflated balloon: what free memory is
        # relative to, and what a shrink must be measured against.
        self.assertEqual(self._client({"actual": 600 * MIB}).balloon_actual_bytes(), 600 * MIB)

    def test_an_unreportable_current_allocation_is_unknown(self):
        from src.virtualizers.qemu.qmp import QMPError

        self.assertIsNone(self._client({}).balloon_actual_bytes())
        self.assertIsNone(self._client({"actual": 0}).balloon_actual_bytes())
        self.assertIsNone(self._client({"actual": "nope"}).balloon_actual_bytes())
        self.assertIsNone(self._client(raises=QMPError("no balloon")).balloon_actual_bytes())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BalloonDeviceArgTests(unittest.TestCase):
    """An unknown -device property is a launch failure, so only offer real ones.

    Naming a property the binary does not have does not degrade gracefully: the
    emulator exits with "Property ... not found" and the instance never starts.
    Every QEMU checked spells the stats interval ``guest-stats-polling-interval``
    (6.2 on Ubuntu 22.04 through 8.2); ``stats-polling-interval`` is only a
    fallback for a build that does not, which is why the name is read off
    ``-device virtio-balloon-pci,help`` instead of assumed either way.
    """

    def _arg(self, properties):
        from src.virtualizers.qemu import execute as qemu_execute

        qemu_execute._balloon_properties.cache_clear()
        try:
            with unittest.mock.patch.object(
                qemu_execute, "_balloon_properties", return_value=frozenset(properties)
            ):
                return qemu_execute._balloon_device_arg("qemu-system-aarch64")
        finally:
            qemu_execute._balloon_properties.cache_clear()

    def test_the_id_is_always_present_so_hotplug_can_address_the_device(self):
        self.assertIn("id=nodo-balloon", self._arg([]))

    def test_a_qemu_with_no_optional_properties_gets_a_bare_device(self):
        self.assertEqual(self._arg([]), "virtio-balloon-pci,id=nodo-balloon")

    def test_the_usual_property_name_is_used_when_that_is_what_exists(self):
        arg = self._arg(["guest-stats-polling-interval"])
        self.assertIn("guest-stats-polling-interval=2", arg)
        # Exactly one interval option.
        self.assertEqual(arg.count("polling-interval"), 1)

    def test_the_fallback_name_is_used_when_that_is_all_there_is(self):
        arg = self._arg(["stats-polling-interval"])
        self.assertIn("stats-polling-interval=2", arg)
        self.assertNotIn("guest-stats-polling-interval", arg)

    def test_only_one_interval_property_is_ever_emitted(self):
        arg = self._arg(["guest-stats-polling-interval", "stats-polling-interval"])
        self.assertEqual(arg.count("polling-interval"), 1)

    def test_free_page_reporting_is_included_when_available(self):
        self.assertIn("free-page-reporting=on", self._arg(["free-page-reporting"]))

    def test_deflate_on_oom_is_never_offered_even_where_qemu_has_it(self):
        # Not an oversight: it lets a guest under its own pressure take balloon
        # pages back, silently voiding a reclaim the node already recorded and
        # charged for, and QEMU raises no event when it happens.
        arg = self._arg(["free-page-reporting", "deflate-on-oom"])
        self.assertNotIn("deflate-on-oom", arg)

    def test_an_unavailable_property_is_never_named(self):
        self.assertNotIn("free-page-reporting", self._arg([]))

    def test_a_probe_failure_degrades_to_the_bare_device_rather_than_guessing(self):
        from src.virtualizers.qemu import execute as qemu_execute

        qemu_execute._balloon_properties.cache_clear()
        try:
            with unittest.mock.patch(
                "subprocess.run", side_effect=OSError("no such binary")
            ):
                arg = qemu_execute._balloon_device_arg("qemu-system-nonexistent")
        finally:
            qemu_execute._balloon_properties.cache_clear()
        self.assertEqual(arg, "virtio-balloon-pci,id=nodo-balloon")


if __name__ == "__main__":
    unittest.main()
