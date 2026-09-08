"""A share's bytes are the exporter's disk, all the way to the host's ceiling.

The claim this pins down is the one that cannot be checked by reading a single
function: that what a shared filesystem occupies ends up on the *exporter's* row
in ``local_instances``, and therefore in the three places that read that row --
what the maintenance tick charges, what ``host_limits`` adds into the host's disk
ceiling, and what the next launch is admitted against.

So this test uses no database double. It creates a real SQLite database with the
node's own schema, registers an instance the way ``local_execution`` does, puts a
real directory on disk with real bytes in it, and then runs the same two calls the
maintenance sweep runs -- reading the answer back out through the node's own
queries.
"""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.utils import host_limits
    from src.utils.shared_filesystems import ShareRef
    from src.virtualizers.microvm import paths, shares, virtiofs
    from src.virtualizers.microvm.runtime_state import save_runtime_state
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc


ROOTFS_BYTES = 500
SHARE_BYTES = 1000
SHARE_ID = "a" * 64
PARENT = "vm-exporter"
CHILD = "vm-guest"


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class ShareDiskAccountingTest(unittest.TestCase):
    """The exporter's row, end to end, over a real database and a real directory."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        # A real database with the node's own schema, in place of the node's.
        self.db = sqlite3.connect(str(Path(self.root) / "test.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        migrate.create_tables(self.db.cursor())
        self.db.commit()
        self.previous_connection = SQLConnection._connection
        SQLConnection._connection = self.db
        # Every path the share machinery resolves hangs off CACHE.
        self.cache_patch = patch.object(paths, "cache_root", return_value=self.root)
        self.cache_patch.start()
        self.sc = SQLConnection()

    def tearDown(self):
        self.cache_patch.stop()
        SQLConnection._connection = self.previous_connection
        self.db.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # -- the world as a launch leaves it ----------------------------------- #

    def _register(self, vmachine_id, disk_space, name):
        """Register an instance the way ``local_execution`` does."""
        self.sc.add_local_instance(
            father_id="dev-client-1",
            container_ip="10.0.0.2",
            container_id=vmachine_id,
            name=name,
            balance_mu=10 ** 9,
            serialized_instance="",
            service_id="svc",
            virtualizer="ch",
            disk_space=disk_space,
            envs=None,
            mem_limit=64 * 1024 ** 2,
        )

    def _rootfs(self, vmachine_id, size=ROOTFS_BYTES):
        image = Path(self.root) / f"{vmachine_id}.ext4"
        image.write_bytes(b"i" * size)
        return image

    def _materialize_share(self, size=SHARE_BYTES):
        """Put a share on disk, reserved by its exporter, with real bytes in it."""
        base = str(virtiofs.shared_fs_base_dir(self.root))
        directory = virtiofs.shared_dir(base, SHARE_ID)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "payload.bin").write_bytes(b"x" * size)
        ref = ShareRef(share_id=SHARE_ID, name="data", env="", discriminator="",
                       path="/data", readonly=False)
        virtiofs.reserve_share(base, virtiofs.mount_for(ref, base, exported=True), PARENT)
        return directory

    def _tick(self, vmachine_id):
        """The two lines the maintenance sweep runs for one instance."""
        resolved = shares.resolved_disk_bytes(vmachine_id)
        if resolved is not None:
            self.sc.update_sys_req(id=vmachine_id, mem_limit=None, disk_space=resolved)
        return resolved

    def _recorded_disk(self, vmachine_id):
        return int(self.sc.get_sys_req(id=vmachine_id)["disk_space"] or 0)

    # -- the tests ---------------------------------------------------------- #

    def test_the_share_lands_on_the_exporter_s_row_and_in_the_host_ceiling(self):
        self._register(PARENT, ROOTFS_BYTES, "exporter")
        image = self._rootfs(PARENT)
        self._materialize_share()
        save_runtime_state(PARENT, {
            "vmachine_id": PARENT,
            "rootfs_path": str(image),
            "exported_shares": [SHARE_ID],
            "virtiofs": [{"share_id_hex": SHARE_ID}],
        })

        # Before the sweep re-derives it, the row holds only the image.
        self.assertEqual(self._recorded_disk(PARENT), ROOTFS_BYTES)
        self.assertEqual(host_limits.committed_resources().disk_bytes, ROOTFS_BYTES)

        self.assertEqual(self._tick(PARENT), ROOTFS_BYTES + SHARE_BYTES)

        # The row now covers the directory as well as the image...
        self.assertEqual(self._recorded_disk(PARENT), ROOTFS_BYTES + SHARE_BYTES)
        # ...and the host's disk ceiling reads that row, so it counts there too.
        self.assertEqual(
            host_limits.committed_resources().disk_bytes, ROOTFS_BYTES + SHARE_BYTES
        )

    def test_growth_after_the_launch_is_picked_up(self):
        # This is the whole reason the figure is re-derived instead of trusted: an
        # image is a fixed-size file and cannot grow, a share directory can.
        self._register(PARENT, ROOTFS_BYTES, "exporter")
        image = self._rootfs(PARENT)
        directory = self._materialize_share()
        save_runtime_state(PARENT, {
            "rootfs_path": str(image), "exported_shares": [SHARE_ID],
        })
        self._tick(PARENT)
        self.assertEqual(self._recorded_disk(PARENT), 1500)

        (directory / "more.bin").write_bytes(b"y" * 2000)
        self._tick(PARENT)
        self.assertEqual(self._recorded_disk(PARENT), 3500)
        self.assertEqual(host_limits.committed_resources().disk_bytes, 3500)

    def test_a_guest_of_the_share_is_charged_nothing_for_it(self):
        # The guest holds the same directory, and none of it is its disk: it
        # declared no ceiling covering it and loses it when the exporter goes.
        self._register(PARENT, ROOTFS_BYTES, "exporter")
        self._register(CHILD, ROOTFS_BYTES, "guest")
        self._materialize_share()
        base = str(virtiofs.shared_fs_base_dir(self.root))
        ref = ShareRef(share_id=SHARE_ID, name="data", env="", discriminator="",
                       path="/mnt/data", readonly=True)
        virtiofs.reserve_share(base, virtiofs.mount_for(ref, base, exported=False), CHILD)
        save_runtime_state(CHILD, {
            "rootfs_path": str(self._rootfs(CHILD)),
            "exported_shares": [],                          # it exports nothing
            "virtiofs": [{"share_id_hex": SHARE_ID}],       # but it does hold it
        })

        self.assertIsNone(self._tick(CHILD))                # not measured at all
        self.assertEqual(self._recorded_disk(CHILD), ROOTFS_BYTES)

    def test_an_ordinary_instance_s_row_is_never_touched(self):
        self._register("vm-plain", ROOTFS_BYTES, "plain")
        save_runtime_state("vm-plain", {"rootfs_path": str(self._rootfs("vm-plain"))})
        self.assertIsNone(self._tick("vm-plain"))
        self.assertEqual(self._recorded_disk("vm-plain"), ROOTFS_BYTES)

    def test_only_the_disk_column_moves(self):
        # The one write this makes into a live instance's accounting must not
        # disturb what it holds of anything else.
        self._register(PARENT, ROOTFS_BYTES, "exporter")
        image = self._rootfs(PARENT)
        self._materialize_share()
        save_runtime_state(PARENT, {
            "rootfs_path": str(image), "exported_shares": [SHARE_ID],
        })
        before = dict(self.sc.get_sys_req(id=PARENT))
        self._tick(PARENT)
        after = dict(self.sc.get_sys_req(id=PARENT))
        self.assertNotEqual(before["disk_space"], after["disk_space"])
        for column in ("mem_limit", "cpu_period", "cpu_quota", "arch"):
            self.assertEqual(before[column], after[column], column)


if __name__ == "__main__":
    unittest.main()
