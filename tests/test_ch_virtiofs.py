"""Unit tests for the virtio-fs shared-filesystem backend.

The pure builders are tested directly; the daemon spawn/teardown orchestration is
dependency-injected so it runs on a host that cannot start microVMs. Nothing here
authorizes anything -- the backend is handed shares that were already granted
(see tests/test_manager_shares.py).
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

try:
    from src.utils.shared_filesystems import ShareRef
    # Load the backend module directly from its file rather than through the
    # package, so this stays a test of virtiofs alone: it depends only on
    # shared_filesystems + paths and loads standalone.
    _VF_PATH = Path(__file__).resolve().parents[1] / "src" / "virtualizers" / "microvm" / "virtiofs.py"
    _spec = importlib.util.spec_from_file_location("virtiofs_under_test", _VF_PATH)
    vf = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(vf)
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    ShareRef = vf = None


def _ref(share_id="a" * 64, name="data", path="/data", readonly=False,
         env="", discriminator=""):
    return ShareRef(share_id=share_id, name=name, env=env,
                    discriminator=discriminator, path=path, readonly=readonly)


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class VirtiofsBuildersTest(unittest.TestCase):
    def test_both_sides_of_a_share_land_on_one_tag_and_one_directory(self):
        exporter = vf.mount_for(_ref(path="/data"), "/base", exported=True)
        # The importer mounts the same share wherever it declared it.
        importer = vf.mount_for(_ref(path="/mnt/hdfs", readonly=True), "/base", exported=False)
        self.assertEqual(exporter.tag, importer.tag)
        self.assertEqual(exporter.host_dir, importer.host_dir)
        self.assertEqual((exporter.guest_path, importer.guest_path), ("/data", "/mnt/hdfs"))
        self.assertFalse(exporter.readonly)   # an exporter always mounts rw
        self.assertTrue(importer.readonly)    # the child asked for ro
        self.assertTrue(exporter.exported)
        self.assertFalse(importer.exported)
        self.assertFalse(exporter.external)

    def test_a_handed_over_directory_is_mounted_where_it_lives(self):
        mount = vf.mount_for(_ref(), "/base", exported=False, host_dir="/home/me/data")
        self.assertEqual(mount.host_dir, "/home/me/data")
        self.assertTrue(mount.external)

    def test_guest_mount_plan_is_stable_json(self):
        mount = vf.SharedMount(
            share_id_hex="deadbeef" * 8, tag="vfs-deadbeefdeadbeef",
            readonly=True, guest_path="/mnt/db", host_dir="/base/x/shared",
        )
        plan = json.loads(vf.build_guest_mount_plan([mount]))
        self.assertEqual(plan, [{"tag": "vfs-deadbeefdeadbeef", "path": "/mnt/db", "ro": True}])

    def test_fs_device_arg(self):
        arg = vf.build_fs_device_arg("vfs-abc", "/tmp/nodo-ch/vfs-abc.sock")
        self.assertIn("tag=vfs-abc", arg)
        self.assertIn("socket=/tmp/nodo-ch/vfs-abc.sock", arg)


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class ShareOwnershipTest(unittest.TestCase):
    """A share is part of the instance that exports it, and ends with it."""

    def _share(self, base, sid="a" * 64):
        ref = _ref(share_id=sid)
        vf.reserve_share(base, vf.mount_for(ref, base, exported=True), "vm-parent")
        vf.reserve_share(base, vf.mount_for(ref, base, exported=False), "vm-child")
        return sid

    def test_a_guest_leaving_does_not_end_the_share(self):
        with tempfile.TemporaryDirectory() as base:
            sid = self._share(base)
            state = vf.release_share(base, sid, "vm-child")
            self.assertFalse(state["spent"])
            self.assertEqual(state["users"], ["vm-parent"])

    def test_the_exporter_leaving_ends_it_even_with_guests_running(self):
        # The guests declared no ceiling for this storage and are not charged for
        # it; there is nothing left to hold it up once its owner is gone.
        with tempfile.TemporaryDirectory() as base:
            sid = self._share(base)
            state = vf.release_share(base, sid, "vm-parent")
            self.assertTrue(state["spent"])
            self.assertEqual(state["users"], ["vm-child"])   # they lose the directory

    def test_a_guest_that_outlived_its_exporter_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as base:
            sid = self._share(base)
            vf.release_share(base, sid, "vm-parent")
            # Whatever is left is owned by nobody; the last one out still ends it.
            self.assertTrue(vf.release_share(base, sid, "vm-child")["spent"])

    def test_the_owner_is_recorded_on_reservation(self):
        with tempfile.TemporaryDirectory() as base:
            sid = self._share(base)
            self.assertEqual(vf.load_share_state(base, sid)["owner"], "vm-parent")


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class VirtiofsOrchestrationTest(unittest.TestCase):
    def test_attach_empty_is_a_noop(self):
        args, state = vf.attach_virtiofs_backends(
            [], "vm-1", base_dir="/base", socket_dir="/sock", virtiofsd_binary="virtiofsd",
        )
        self.assertEqual((args, state), ([], []))

    def test_ensure_backend_spawns_then_reuses(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            sid = "a" * 64
            mount = vf.mount_for(_ref(share_id=sid), base, exported=True)
            spawned = []
            kwargs = dict(
                base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=lambda cmd, log_path: spawned.append(cmd) or 4242,
                pid_alive_fn=lambda pid: True,
            )
            state = vf.ensure_share_backend(mount, "vm-1", **kwargs)
            self.assertEqual(state["pid"], 4242)
            self.assertTrue(Path(vf.shared_dir(base, sid)).is_dir())
            self.assertEqual(len(spawned), 1)

            # Socket must exist for reuse; simulate the daemon having bound it.
            Path(state["socket"]).write_text("")
            vf.ensure_share_backend(mount, "vm-2", **kwargs)
            self.assertEqual(len(spawned), 1)  # reused, not spawned again
            self.assertEqual(vf.load_share_state(base, sid)["users"], ["vm-1", "vm-2"])

    def test_export_is_seeded_once_and_never_by_a_child(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            sid = "d" * 64
            export = vf.mount_for(_ref(share_id=sid), base, exported=True)
            seeded = []

            def fake_seed(mount, dest):
                seeded.append(mount.guest_path)
                (Path(dest) / "packaged.txt").write_text("from the image")

            kwargs = dict(
                base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=lambda cmd, log_path: 1, pid_alive_fn=lambda pid: False,
                seed_fn=fake_seed,
            )
            vf.ensure_share_backend(export, "vm-parent", **kwargs)
            self.assertEqual(seeded, ["/data"])
            self.assertEqual(
                (vf.shared_dir(base, sid) / "packaged.txt").read_text(), "from the image"
            )

            # Already materialized: neither a second boot of the exporter nor a
            # child attaching re-seeds over what the share now holds.
            vf.ensure_share_backend(export, "vm-parent", **kwargs)
            vf.ensure_share_backend(
                vf.mount_for(_ref(share_id=sid), base, exported=False), "vm-child", **kwargs
            )
            self.assertEqual(seeded, ["/data"])

    def test_a_handed_over_directory_is_never_seeded(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock, \
             tempfile.TemporaryDirectory() as host:
            mount = vf.mount_for(_ref(share_id="e" * 64), base, exported=True, host_dir=host)
            seeded = []
            vf.ensure_share_backend(
                mount, "vm-dev", base_dir=base, socket_dir=sock,
                virtiofsd_binary="virtiofsd", spawn_fn=lambda c, l: 1,
                pid_alive_fn=lambda pid: False, seed_fn=lambda m, d: seeded.append(d),
            )
            self.assertEqual(seeded, [])


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class VirtiofsTeardownTest(unittest.TestCase):
    def _materialize(self, base, sid, *users):
        for vmachine_id, exported in users:
            mount = vf.mount_for(_ref(share_id=sid), base, exported=exported)
            vf.shared_dir(base, sid).mkdir(parents=True, exist_ok=True)
            vf.reserve_share(base, mount, vmachine_id)
        path = vf.share_state_path(base, sid)
        state = vf.load_share_state(base, sid)
        state.update({"pid": 999, "socket": str(Path(base) / "s.sock")})
        vf._save_share_state(path, state)

    def test_the_daemon_survives_a_guest_leaving(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "b" * 64
            self._materialize(base, sid, ("vm-parent", True), ("vm-child", False))
            killed = []
            vf.teardown_virtiofs_for_vm(
                "vm-child", [{"share_id_hex": sid}], base_dir=base, kill_fn=killed.append,
            )
            self.assertEqual(killed, [])
            self.assertTrue(vf.share_state_dir(base, sid).exists())

    def test_the_exporter_leaving_takes_the_daemon_and_the_data(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "c" * 64
            self._materialize(base, sid, ("vm-parent", True), ("vm-child", False))
            killed, logged = [], []
            vf.teardown_virtiofs_for_vm(
                "vm-parent", [{"share_id_hex": sid}], base_dir=base,
                kill_fn=killed.append, logger_fn=logged.append,
            )
            self.assertEqual(killed, [999])
            self.assertFalse(vf.share_state_dir(base, sid).exists())
            # The guests that lose it are named, not left to be inferred.
            self.assertTrue(any("still running" in line for line in logged))

    def test_a_handed_over_directory_is_released_but_never_deleted(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as host:
            sid = "f" * 64
            mount = vf.mount_for(_ref(share_id=sid), base, exported=False, host_dir=host)
            vf.reserve_share(base, mount, "vm-dev")
            (Path(host) / "mine.txt").write_text("the developer's own")
            vf.teardown_virtiofs_for_vm(
                "vm-dev", [{"share_id_hex": sid, "external": True}], base_dir=base,
                kill_fn=lambda pid: None,
            )
            self.assertTrue((Path(host) / "mine.txt").is_file())


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class ShareBytesTest(unittest.TestCase):
    def test_the_bytes_a_share_holds_are_measurable(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "a" * 64
            data = vf.shared_dir(base, sid)
            (data / "sub").mkdir(parents=True)
            (data / "a.bin").write_bytes(b"x" * 100)
            (data / "sub" / "b.bin").write_bytes(b"y" * 50)
            self.assertEqual(vf.share_bytes(base, [sid]), 150)
            self.assertEqual(vf.share_bytes(base, []), 0)
            self.assertEqual(vf.share_bytes(base, ["z" * 64]), 0)


if __name__ == "__main__":
    unittest.main()
