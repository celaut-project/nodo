"""`nodo prune` has to reclaim the microVM cache disk no other command reclaims.

`nodo remove <service>` frees the bundle of a service somebody names. Two trees
under `CACHE/microvm/` grow with no owner at all:

* `runtime/<vmachine_id>/`, freed by `kill` -- so what is left there is precisely
  what `kill` did not finish freeing. `kill` removes the directory first and the
  state file second, so a teardown interrupted between the two leaves a full
  rootfs image behind a *missing* state file: invisible to the janitor, which
  starts from the state files.
* `failures/<vmachine_id>/`, written on purpose for debugging and pruned by
  nobody -- 1.9 GB on the node this was filed from.

The risk in this command is the opposite of leaking: deleting the runtime
directory of a VM that is alive, or of a launch still unpacking its image. Both
are tested here.
"""
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.microvm import maintain as microvm_maintain
    from src.virtualizers.microvm import paths as microvm_paths
    from src.commands import prune as prune_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    microvm_maintain = None  # type: ignore[assignment]
    microvm_paths = None  # type: ignore[assignment]
    prune_cmd = None  # type: ignore[assignment]

DAY = 86400.0


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CacheFixture(unittest.TestCase):
    """A cache directory shaped like a node that has been running for a while."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.family = self.cache / microvm_paths.FAMILY_DIR_NAME
        self.runtime = self.family / "runtime"
        self.failures = self.family / "failures"
        self.runtime.mkdir(parents=True)
        self.failures.mkdir(parents=True)

        # Every family path derives from one root, so one patch redirects the
        # whole layout -- the runtime tree, the failures tree and the sizes read
        # off both.
        patcher = patch.object(microvm_paths, "cache_root", return_value=str(self.cache))
        patcher.start()
        self.addCleanup(patcher.stop)

        from src.virtualizers.microvm import build as microvm_build

        patcher = patch.object(microvm_build, "CACHE", str(self.cache))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_runtime_dir(self, vmachine_id, size=1024, age_days=None):
        path = self.runtime / vmachine_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "rootfs.ext4").write_bytes(b"0" * size)
        if age_days is not None:
            stamp = time.time() - age_days * DAY
            import os

            os.utime(path, (stamp, stamp))
        return path

    def _write_failure(self, vmachine_id, size=2048, age_days=0.0):
        path = self.failures / vmachine_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "rootfs.ext4").write_bytes(b"0" * size)
        (path / "error.txt").write_text("boom")
        stamp = time.time() - age_days * DAY
        import os

        os.utime(path, (stamp, stamp))
        return path


class OrphanRuntimeScanTests(CacheFixture):
    def _scan(self, states, *, in_db=True, alive=True):
        with patch.object(
            microvm_maintain, "list_runtime_states", return_value=states
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=in_db
        ), patch.object(
            microvm_maintain, "_state_is_alive", return_value=alive
        ):
            return microvm_maintain.scan_orphan_runtimes()

    def test_a_healthy_vm_is_never_offered_for_pruning(self):
        self._write_runtime_dir("vm-live")

        entries = self._scan({"vm-live": {"pid": 42}}, in_db=True, alive=True)

        self.assertEqual(entries, [])

    def test_an_orphaned_state_is_reported_with_its_size(self):
        self._write_runtime_dir("vm-orphan", size=4096)

        entries = self._scan({"vm-orphan": {"pid": 42}}, in_db=False, alive=True)

        self.assertEqual([e.vmachine_id for e in entries], ["vm-orphan"])
        self.assertEqual(entries[0].reason, "orphan_runtime_state")
        self.assertEqual(entries[0].size_bytes, 4096)

    def test_a_directory_whose_state_file_is_gone_is_the_leak_this_catches(self):
        # `kill` deletes the directory, then the state. Interrupted in between,
        # this is what is left -- and the janitor, which iterates state files,
        # can never see it.
        self._write_runtime_dir("vm-stateless", size=8192, age_days=3)

        entries = self._scan({})

        self.assertEqual([e.vmachine_id for e in entries], ["vm-stateless"])
        self.assertEqual(entries[0].reason, "runtime_dir_without_state")
        self.assertEqual(entries[0].size_bytes, 8192)

    def test_a_launch_still_unpacking_its_image_is_left_alone(self):
        # `execute` creates the directory before it writes any state. A prune that
        # deleted it there would destroy the image of a VM that is starting.
        self._write_runtime_dir("vm-starting", age_days=0)

        entries = self._scan({})

        self.assertEqual(entries, [])

    def test_a_booting_vm_keeps_the_janitors_grace(self):
        # Same rule as the janitor, because it is literally the same function.
        self._write_runtime_dir("vm-booting")

        entries = self._scan({"vm-booting": {"pid": 42, "booting": True}}, in_db=False, alive=True)

        self.assertEqual(entries, [])

    def test_entries_are_ordered_by_the_disk_they_hold(self):
        self._write_runtime_dir("vm-small", size=100, age_days=3)
        self._write_runtime_dir("vm-big", size=9000, age_days=3)

        entries = self._scan({})

        self.assertEqual([e.vmachine_id for e in entries], ["vm-big", "vm-small"])


class FailureScanTests(CacheFixture):
    def test_entries_older_than_the_window_are_reclaimable(self):
        self._write_failure("vm-old", size=4096, age_days=30)

        prunable, kept = microvm_maintain.scan_failures(retention_seconds=7 * DAY)

        self.assertEqual([e.vmachine_id for e in prunable], ["vm-old"])
        self.assertEqual(kept, [])

    def test_a_recent_failure_is_kept_and_says_why(self):
        # The debris of the launch an operator is investigating right now.
        self._write_failure("vm-fresh", age_days=1)

        prunable, kept = microvm_maintain.scan_failures(retention_seconds=7 * DAY)

        self.assertEqual(prunable, [])
        self.assertEqual([e.vmachine_id for e in kept], ["vm-fresh"])
        self.assertIn("within_retention_window", kept[0].reason)

    def test_all_takes_everything_regardless_of_age(self):
        self._write_failure("vm-fresh", age_days=0)
        self._write_failure("vm-old", age_days=30)

        prunable, kept = microvm_maintain.scan_failures(retention_seconds=None)

        self.assertEqual({e.vmachine_id for e in prunable}, {"vm-fresh", "vm-old"})
        self.assertEqual(kept, [])

    def test_a_node_that_never_failed_a_launch_reports_nothing(self):
        prunable, kept = microvm_maintain.scan_failures(retention_seconds=7 * DAY)

        self.assertEqual((prunable, kept), ([], []))


class ReclaimTests(CacheFixture):
    def test_a_stateless_directory_is_simply_deleted(self):
        path = self._write_runtime_dir("vm-stateless", size=2048, age_days=3)
        entry = microvm_maintain.PruneEntry(
            kind="runtime",
            vmachine_id="vm-stateless",
            path=path,
            reason="runtime_dir_without_state",
            size_bytes=2048,
        )

        with patch.object(microvm_maintain, "kill_vm") as kill_vm:
            microvm_maintain.reclaim(entry)

        kill_vm.assert_not_called()
        self.assertFalse(path.exists())
        self.assertTrue(entry.removed)
        self.assertEqual(entry.size_bytes, 2048)

    def test_an_orphaned_vm_goes_through_kill_not_rmtree(self):
        # The directory is not the only thing it left behind: there is a tap
        # device, a cgroup, a control socket and firewall rules. Only `kill`
        # frees those, so reclaiming the disk directly would leak the rest.
        path = self._write_runtime_dir("vm-orphan", size=1024)
        entry = microvm_maintain.PruneEntry(
            kind="runtime",
            vmachine_id="vm-orphan",
            path=path,
            reason="orphan_runtime_state",
            size_bytes=1024,
        )
        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 1, "virtualizer": "qemu"},
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            microvm_maintain, "kill_vm", return_value=True
        ) as kill_vm:
            microvm_maintain.reclaim(entry)

        # Torn down as what it is, not as CH: the entry names its hypervisor.
        kill_vm.assert_called_once_with(
            microvm_maintain.member("qemu"), vmachine_id="vm-orphan"
        )
        self.assertTrue(entry.removed)

    def test_an_entry_no_hypervisor_claims_is_reported_not_deleted(self):
        path = self._write_runtime_dir("vm-alien", size=1024)
        entry = microvm_maintain.PruneEntry(
            kind="runtime",
            vmachine_id="vm-alien",
            path=path,
            reason="orphan_runtime_state",
            size_bytes=1024,
        )

        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 1, "virtualizer": "firecracker"},
        ), patch.object(
            microvm_maintain, "kill_vm"
        ) as kill_vm:
            microvm_maintain.reclaim(entry)

        kill_vm.assert_not_called()
        self.assertFalse(entry.removed)
        self.assertIn("firecracker", entry.error)
        self.assertTrue(path.exists())

    def test_a_registered_instance_is_stopped_so_its_deposit_goes_home(self):
        # The case that made this routing necessary: a guest whose kernel
        # panicked is an orphan whose process *and* database row are both alive.
        # `kill` would free the host's side of it and leave the row holding a
        # deposit nobody is spending, reconciled only by a maintenance tick that
        # never runs on a node whose `serve` is stopped (#326).
        path = self._write_runtime_dir("vm-registered", size=1024)
        entry = microvm_maintain.PruneEntry(
            kind="runtime",
            vmachine_id="vm-registered",
            path=path,
            reason="guest_panicked",
            size_bytes=1024,
        )

        rows = {"vm-registered"}

        def stop_instance(token):
            rows.discard(token)

        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 1, "virtualizer": "qemu"},
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", side_effect=lambda id: id in rows
        ), patch.object(
            microvm_maintain, "kill_vm"
        ) as kill_vm, patch(
            "src.manager.manager.stop_instance", side_effect=stop_instance
        ) as stop:
            microvm_maintain.reclaim(entry)

        stop.assert_called_once_with(token="vm-registered")
        # `stop_instance` kills the VM itself; a second teardown here would be
        # `kill` running against a torn-down VM.
        kill_vm.assert_not_called()
        self.assertTrue(entry.removed)

    def test_an_unregistered_orphan_is_only_killed(self):
        # No row means no deposit to return and nothing to purge, so the
        # host-side teardown is the whole job.
        path = self._write_runtime_dir("vm-rowless", size=1024)
        entry = microvm_maintain.PruneEntry(
            kind="runtime",
            vmachine_id="vm-rowless",
            path=path,
            reason="orphan_runtime_state",
            size_bytes=1024,
        )

        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 1, "virtualizer": "qemu"},
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            microvm_maintain, "kill_vm", return_value=True
        ) as kill_vm, patch(
            "src.manager.manager.stop_instance"
        ) as stop:
            microvm_maintain.reclaim(entry)

        stop.assert_not_called()
        kill_vm.assert_called_once_with(
            microvm_maintain.member("qemu"), vmachine_id="vm-rowless"
        )
        self.assertTrue(entry.removed)

    def test_a_stop_that_left_the_row_behind_is_not_reported_as_done(self):
        # `stop_instance` swallows a failed purge and returns None. Taking that
        # for a stop would retire an instance from the operator's report while
        # its balance stays on the books.
        path = self._write_runtime_dir("vm-stubborn", size=1024)
        entry = microvm_maintain.PruneEntry(
            kind="runtime",
            vmachine_id="vm-stubborn",
            path=path,
            reason="guest_panicked",
            size_bytes=1024,
        )

        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 1, "virtualizer": "qemu"},
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=True
        ), patch.object(
            microvm_maintain, "kill_vm"
        ), patch(
            "src.manager.manager.stop_instance", return_value=None
        ):
            microvm_maintain.reclaim(entry)

        self.assertFalse(entry.removed)
        self.assertIn("database row", entry.error)
        # Reported as what it is: still there, and still holding its disk.
        self.assertTrue(path.exists())
        self.assertEqual(entry.size_bytes, 0)

    def test_an_unreadable_database_stops_the_reclamation(self):
        # Guessing "not registered" and killing would leave a father charged for
        # a child that is gone, which is the one direction the books must not
        # err in. Nothing is touched and the entry says why.
        path = self._write_runtime_dir("vm-unknowable", size=1024)
        entry = microvm_maintain.PruneEntry(
            kind="runtime",
            vmachine_id="vm-unknowable",
            path=path,
            reason="guest_panicked",
            size_bytes=1024,
        )

        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 1, "virtualizer": "qemu"},
        ), patch.object(
            microvm_maintain.sc,
            "internal_instance_exists",
            side_effect=RuntimeError("no such table: local_instances"),
        ), patch.object(
            microvm_maintain, "kill_vm"
        ) as kill_vm:
            microvm_maintain.reclaim(entry)

        kill_vm.assert_not_called()
        self.assertFalse(entry.removed)
        self.assertIn("registered", entry.error)
        self.assertTrue(path.exists())

    def test_a_failed_removal_reports_what_it_actually_freed(self):
        # Never claim disk that is still on disk.
        path = self._write_runtime_dir("vm-stuck", size=4096, age_days=3)
        entry = microvm_maintain.PruneEntry(
            kind="failure",
            vmachine_id="vm-stuck",
            path=path,
            reason="failure_debris",
            size_bytes=4096,
        )

        with patch.object(microvm_maintain.shutil, "rmtree", side_effect=OSError("device busy")):
            microvm_maintain.reclaim(entry)

        self.assertFalse(entry.removed)
        self.assertIn("device busy", entry.error)
        self.assertEqual(entry.size_bytes, 0)
        self.assertTrue(path.exists())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PruneCommandTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(prune_cmd.os, "geteuid", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entry(self, **kwargs):
        from src.virtualizers.microvm.maintain import PruneEntry

        defaults = dict(
            kind="runtime",
            vmachine_id="vm-1",
            path=Path("/tmp/vm-1"),
            reason="orphan_runtime_state",
            size_bytes=1_500_000_000,
            age_seconds=3 * DAY,
        )
        defaults.update(kwargs)
        return PruneEntry(**defaults)

    def _run(self, argv, runtimes=(), failures=(), kept=()):
        output = io.StringIO()

        def fake_reclaim(entry):
            entry.removed = True
            return entry

        with patch(
            "src.virtualizers.microvm.maintain.scan_orphan_runtimes", return_value=list(runtimes)
        ), patch(
            "src.virtualizers.microvm.maintain.scan_failures",
            return_value=(list(failures), list(kept)),
        ), patch(
            "src.virtualizers.microvm.maintain.reclaim", side_effect=fake_reclaim
        ) as reclaim, contextlib.redirect_stdout(
            output
        ):
            prune_cmd.prune(argv=argv)
        return output.getvalue(), reclaim

    def test_it_reports_the_disk_it_freed(self):
        printed, reclaim = self._run([], runtimes=[self._entry()])

        reclaim.assert_called_once()
        self.assertIn("1.40 GB", printed)
        self.assertIn("Freed", printed)

    def test_dry_run_removes_nothing(self):
        printed, reclaim = self._run(["--dry-run"], runtimes=[self._entry()])

        reclaim.assert_not_called()
        self.assertIn("Dry run", printed)
        self.assertIn("Nothing was removed", printed)

    def test_dry_run_needs_no_root(self):
        # Reading disk is not a privileged act, and an operator hunting for space
        # should not have to sudo to be told there is none.
        with patch.object(prune_cmd.os, "geteuid", return_value=1000):
            printed, reclaim = self._run(["--dry-run"], runtimes=[self._entry()])

        reclaim.assert_not_called()
        self.assertNotIn("superuser", printed)

    def test_without_root_a_real_prune_refuses(self):
        with patch.object(prune_cmd.os, "geteuid", return_value=1000):
            printed, reclaim = self._run([], runtimes=[self._entry()])

        reclaim.assert_not_called()
        self.assertIn("superuser", printed)

    def test_kept_failures_are_shown_not_silently_skipped(self):
        kept = self._entry(
            kind="failure",
            vmachine_id="vm-fresh",
            reason="within_retention_window (7d)",
            size_bytes=900_000_000,
        )

        printed, _ = self._run([], kept=[kept])

        self.assertIn("vm-fresh", printed)
        self.assertIn("858.31 MB", printed)
        self.assertIn("--all", printed)

    def test_an_entry_that_could_not_be_removed_is_called_out(self):
        entry = self._entry()

        output = io.StringIO()

        def failing_reclaim(e):
            e.removed = False
            e.error = "device busy"
            e.size_bytes = 0
            return e

        with patch(
            "src.virtualizers.microvm.maintain.scan_orphan_runtimes", return_value=[entry]
        ), patch(
            "src.virtualizers.microvm.maintain.scan_failures", return_value=([], [])
        ), patch(
            "src.virtualizers.microvm.maintain.reclaim", side_effect=failing_reclaim
        ), contextlib.redirect_stdout(
            output
        ):
            prune_cmd.prune(argv=[])

        printed = output.getvalue()
        self.assertIn("device busy", printed)
        self.assertIn("could not be fully removed", printed)

    def test_a_clean_node_says_so(self):
        printed, reclaim = self._run([])

        reclaim.assert_not_called()
        self.assertIn("Nothing to prune", printed)

    def test_an_unknown_flag_is_refused_rather_than_ignored(self):
        # A typo'd flag must not silently become a full prune.
        printed, reclaim = self._run(["--dry-runn"], runtimes=[self._entry()])

        reclaim.assert_not_called()
        self.assertIn("Unknown argument", printed)
        self.assertIn("Usage:", printed)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RetentionConfigTests(unittest.TestCase):
    def test_the_window_comes_from_config(self):
        with patch.object(prune_cmd.env_manager, "get", return_value=30):
            self.assertEqual(prune_cmd._retention_days(), 30.0)

    def test_a_nonsense_window_falls_back_loudly(self):
        for bad in ("soon", None, -5):
            with self.subTest(bad=bad):
                output = io.StringIO()
                with patch.object(
                    prune_cmd.env_manager, "get", return_value=bad
                ), contextlib.redirect_stdout(output):
                    days = prune_cmd._retention_days()
                self.assertEqual(days, float(prune_cmd.DEFAULT_FAILURE_RETENTION_DAYS))
                self.assertIn("Warning", output.getvalue())


if __name__ == "__main__":
    import unittest.mock  # noqa: F401  (used above via unittest.mock)

    unittest.main()
