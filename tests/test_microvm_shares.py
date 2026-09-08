"""The one place both hypervisors materialize a guest's shared filesystems.

Whatever a backend does with the result afterwards -- cloud-hypervisor splices
the `--fs` arguments in, QEMU builds vhost-user-fs devices off the mount state --
the resolving, granting, seeding and mount-plan injection happen here, once.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from protos import celaut_pb2 as celaut
    from src.manager.shares import ShareAuthorizationError
    from src.utils.shared_filesystems import ShareRef, exported_refs
    from src.virtualizers.microvm import shares
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = shares = ShareRef = exported_refs = ShareAuthorizationError = None


def _dir(name, xattrs=None, children=None):
    b = celaut.Service.Container.Filesystem.ItemBranch(name=name)
    b.filesystem.SetInParent()
    for c in (children or []):
        b.filesystem.branch.append(c)
    for k, v in (xattrs or {}).items():
        b.xattrs[k] = v
    return b


def _service(*branches):
    s = celaut.Service()
    fs = celaut.Service.Container.Filesystem()
    for b in branches:
        fs.branch.append(b)
    s.container.filesystem = fs.SerializeToString()
    return s


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class MaterializeSharesTest(unittest.TestCase):
    def _run(self, service, father_id="", granted=None, config=None):
        """Call materialize_shares with the host side stubbed out, and report
        what it asked the backend and the image to do.

        ``granted`` is what authorization returned; whether it *should* have is
        tests/test_manager_shares.py's subject, not this one's.
        """
        seen = {}

        def fake_attach(mounts, vmachine_id, **kwargs):
            seen["mounts"] = mounts
            seen["vmachine_id"] = vmachine_id
            seen["seed_fn"] = kwargs["seed_fn"]
            return ["--fs", "device"], [
                {"share_id_hex": m.share_id_hex} for m in mounts
            ]

        with tempfile.TemporaryDirectory() as runtime_dir:
            with patch.object(shares, "attach_virtiofs_backends", side_effect=fake_attach), \
                 patch.object(shares, "authorize_shares", return_value=granted or []), \
                 patch.object(shares, "rundev_host_dirs", return_value={}), \
                 patch.object(shares.rootfs, "debugfs_write") as write, \
                 patch.object(shares.paths, "cache_root", return_value=runtime_dir):
                setup = shares.materialize_shares(
                    service=service,
                    config=config,
                    vmachine_id="vm-self",
                    father_id=father_id,
                    rootfs_path=Path(runtime_dir) / "rootfs.ext4",
                    runtime_dir=Path(runtime_dir),
                    log_prefix="[TEST][vm-self]",
                )
                seen["injection"] = write.call_args
                plan = Path(runtime_dir) / shares.GUEST_MOUNT_PLAN_PATH.lstrip("/")
                seen["plan"] = json.loads(plan.read_text()) if plan.is_file() else None
        return setup, seen

    def test_an_ordinary_service_is_a_complete_no_op(self):
        setup, seen = self._run(_service(_dir("mnt", children=[_dir("photos")])))
        self.assertEqual(setup, shares.NO_SHARES)
        self.assertFalse(setup.any)
        self.assertNotIn("mounts", seen)      # no backend was started
        self.assertIsNone(seen["injection"])  # nothing written into the image
        self.assertIsNone(seen["plan"])

    def test_an_exporter_gets_devices_a_mount_plan_and_owns_the_share(self):
        setup, seen = self._run(_service(_dir("data", {"shared": b"true"})))
        self.assertEqual(setup.fs_device_args, ["--fs", "device"])
        self.assertTrue(setup.any)
        # It owns what it exports, which is what lets its teardown remove the data.
        self.assertEqual(
            setup.exported_share_ids, [m["share_id_hex"] for m in setup.mounts_state]
        )
        self.assertEqual(seen["plan"], [{"tag": seen["mounts"][0].tag, "path": "/data", "ro": False}])
        self.assertEqual(
            seen["injection"].kwargs["guest_target"], shares.GUEST_MOUNT_PLAN_PATH
        )
        # The seed reads the exporter's own packaged subtree at that path.
        with patch.object(shares.rootfs, "debugfs_rdump") as rdump:
            seen["seed_fn"](seen["mounts"][0], Path("/dest"))
        self.assertEqual(rdump.call_args.args[1:], ("/data", Path("/dest")))

    def test_an_inherited_share_is_mounted_where_the_child_declared_it(self):
        # The coordinator exports /data as `hdfs-data`; the datanode mounts that
        # same share at /mnt/hdfs.
        exporter = _service(_dir("data", {"shared": b"true", "share_tag": b"hdfs-data"}))
        parent_setup, _ = self._run(exporter)
        [export_ref] = exported_refs(exporter, "vm-self")

        child = _service(_dir("mnt", children=[
            _dir("hdfs", {"guest": b"true", "share_tag": b"hdfs-data"})
        ]))
        inherited = ShareRef(
            share_id=export_ref.share_id, name="hdfs-data", env="", discriminator="",
            path="/mnt/hdfs", readonly=True,
        )
        setup, seen = self._run(child, father_id="vm-parent", granted=[inherited])

        self.assertEqual(
            [m["share_id_hex"] for m in setup.mounts_state], parent_setup.exported_share_ids
        )
        self.assertEqual(seen["plan"], [
            {"tag": seen["mounts"][0].tag, "path": "/mnt/hdfs", "ro": True}
        ])
        # A guest of a share never exports it, so its teardown never owns it.
        self.assertEqual(setup.exported_share_ids, [])

    def test_the_reserving_vm_is_the_one_being_built(self):
        # The share is reserved under this VM's id before its process exists,
        # which is what stops a parent's teardown from deleting it mid-boot.
        _setup, seen = self._run(_service(_dir("data", {"shared": b"true"})))
        self.assertEqual(seen["vmachine_id"], "vm-self")

    def test_a_refused_share_stops_the_launch_instead_of_starting_bare(self):
        # `guest` is an execution precondition: authorization raising is the
        # whole answer, and nothing is materialized.
        child = _service(_dir("data", {"guest": b"true"}))
        with tempfile.TemporaryDirectory() as runtime_dir:
            with patch.object(shares, "authorize_shares",
                              side_effect=ShareAuthorizationError("not exported")), \
                 patch.object(shares, "attach_virtiofs_backends",
                              side_effect=AssertionError("must not attach")), \
                 patch.object(shares.paths, "cache_root", return_value=runtime_dir):
                with self.assertRaises(ShareAuthorizationError):
                    shares.materialize_shares(
                        service=child, config=None, vmachine_id="vm-self",
                        father_id="vm-parent",
                        rootfs_path=Path(runtime_dir) / "rootfs.ext4",
                        runtime_dir=Path(runtime_dir), log_prefix="[TEST]",
                    )


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class ShareAccountingTest(unittest.TestCase):
    """A share is storage of the instance that exports it, undivided.

    Its guests declared no ceiling covering it and are charged nothing for it:
    they use the directory while it lasts and lose it when the exporter goes. So
    the whole figure lands on the exporter's row, which is what the maintenance
    tick charges, what the host's disk ceiling adds up, and what the next launch
    is admitted against.
    """

    def _materialize(self, root, share_id, occupied_bytes, exporter="vm-parent"):
        from src.utils.shared_filesystems import ShareRef
        from src.virtualizers.microvm import virtiofs as vf
        base = str(vf.shared_fs_base_dir(root))
        data = vf.shared_dir(base, share_id)
        data.mkdir(parents=True, exist_ok=True)
        (data / "blob.bin").write_bytes(b"x" * occupied_bytes)
        ref = ShareRef(share_id=share_id, name="data", env="", discriminator="",
                       path="/data", readonly=False)
        vf.reserve_share(base, vf.mount_for(ref, base, exported=True), exporter)
        vf.reserve_share(base, vf.mount_for(ref, base, exported=False), "vm-child")
        return base

    def _state(self, root, rootfs_bytes, exported):
        image = Path(root) / "rootfs.ext4"
        image.write_bytes(b"i" * rootfs_bytes)
        return {"exported_shares": exported, "rootfs_path": str(image)}

    def test_the_exporter_holds_the_whole_share(self):
        with tempfile.TemporaryDirectory() as root:
            self._materialize(root, "a" * 64, 1000)
            with patch.object(shares, "load_runtime_state",
                              return_value=self._state(root, 500, ["a" * 64])),                  patch.object(shares.paths, "cache_root", return_value=root):
                self.assertEqual(shares.exported_disk_bytes("vm-parent"), 1000)
                # rootfs + share: what its row must say for the ceiling to see it.
                self.assertEqual(shares.resolved_disk_bytes("vm-parent"), 1500)

    def test_a_guest_is_charged_nothing_for_it(self):
        with tempfile.TemporaryDirectory() as root:
            self._materialize(root, "b" * 64, 1000)
            # A guest exports nothing, so it holds no share disk of its own.
            with patch.object(shares, "load_runtime_state",
                              return_value=self._state(root, 500, [])),                  patch.object(shares.paths, "cache_root", return_value=root):
                self.assertEqual(shares.exported_disk_bytes("vm-child"), 0)
                self.assertIsNone(shares.resolved_disk_bytes("vm-child"))

    def test_growth_is_re_derived_rather_than_trusted(self):
        # An image cannot grow; a share directory can. That is the whole reason
        # the figure is recomputed instead of read off the row.
        with tempfile.TemporaryDirectory() as root:
            base = self._materialize(root, "c" * 64, 100)
            state = self._state(root, 500, ["c" * 64])
            with patch.object(shares, "load_runtime_state", return_value=state), \
                 patch.object(shares.paths, "cache_root", return_value=root):
                self.assertEqual(shares.resolved_disk_bytes("vm-parent"), 600)
                from src.virtualizers.microvm import virtiofs as vf
                (vf.shared_dir(base, "c" * 64) / "more.bin").write_bytes(b"y" * 400)
                self.assertEqual(shares.resolved_disk_bytes("vm-parent"), 1000)

    def test_an_instance_with_no_shares_is_never_measured(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(shares, "load_runtime_state", return_value={}), \
                 patch.object(shares.paths, "cache_root", return_value=root):
                self.assertIsNone(shares.resolved_disk_bytes("vm-plain"))
                self.assertEqual(shares.exported_disk_bytes("vm-plain"), 0)
