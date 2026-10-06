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

    def test_each_vm_has_its_own_socket_for_a_share(self):
        # A virtiofsd answers one client, so two VMs on one share need two sockets.
        sid = "a" * 64
        parent = vf.virtiofs_socket_path("/tmp/nodo-ch", sid, "1" * 64)
        child = vf.virtiofs_socket_path("/tmp/nodo-ch", sid, "2" * 64)
        other_share = vf.virtiofs_socket_path("/tmp/nodo-ch", "b" * 64, "1" * 64)
        self.assertEqual(len({parent, child, other_share}), 3)

    def test_the_socket_path_fits_in_sun_path(self):
        # AF_UNIX paths are limited to 108 bytes including the terminator.
        path = vf.virtiofs_socket_path("/tmp/nodo-ch", "f" * 64, "e" * 64)
        self.assertLess(len(str(path).encode()), 108)


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

    def test_two_shares_are_one_fs_option_with_two_values(self):
        # cloud-hypervisor declares `--fs <fs>...` as one option taking several values and
        # refuses it repeated, so `--fs a --fs b` cannot start a guest with two shares.
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            mounts = [
                vf.mount_for(_ref(share_id=sid * 64), base, exported=True) for sid in ("a", "b")
            ]
            args, state = vf.attach_virtiofs_backends(
                mounts, "vm-1", base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=lambda cmd, log_path: 4242, pid_alive_fn=lambda pid: True,
            )
            self.assertEqual(args.count("--fs"), 1)
            self.assertEqual(args[0], "--fs")
            self.assertEqual(len(args), 3)
            self.assertEqual(len(state), 2)
            self.assertTrue(all(value.startswith("tag=") for value in args[1:]))

    def test_one_share_is_still_a_single_fs_option(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            mount = vf.mount_for(_ref(share_id="c" * 64), base, exported=True)
            args, _state = vf.attach_virtiofs_backends(
                [mount], "vm-1", base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=lambda cmd, log_path: 4242, pid_alive_fn=lambda pid: True,
            )
            self.assertEqual(len(args), 2)
            self.assertEqual(args[0], "--fs")

    def test_each_vm_gets_a_daemon_of_its_own_on_the_same_directory(self):
        # A virtiofsd serves a single vhost-user client: a second VM connected to the
        # parent's socket is never answered and its guest never boots (#480).
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            sid = "a" * 64
            spawned = []
            pids = iter([4242, 4343])
            kwargs = dict(
                base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=lambda cmd, log_path: spawned.append(cmd) or next(pids),
                pid_alive_fn=lambda pid: True,
            )
            parent = vf.ensure_share_backend(
                vf.mount_for(_ref(share_id=sid), base, exported=True), "vm-parent", **kwargs)
            Path(parent["socket"]).write_text("")   # the daemon bound its socket
            child = vf.ensure_share_backend(
                vf.mount_for(_ref(share_id=sid), base, exported=False), "vm-child", **kwargs)

            self.assertEqual(len(spawned), 2)
            self.assertNotEqual(parent["socket"], child["socket"])
            self.assertEqual((parent["pid"], child["pid"]), (4242, 4343))
            # Both daemons export the one host directory.
            shared = str(vf.shared_dir(base, sid))
            for command in spawned:
                self.assertEqual(command[command.index("--shared-dir") + 1], shared)
            state = vf.load_share_state(base, sid)
            self.assertEqual(state["users"], ["vm-parent", "vm-child"])
            self.assertEqual(state["daemons"]["vm-parent"], {"pid": 4242, "socket": parent["socket"]})
            self.assertEqual(state["daemons"]["vm-child"], {"pid": 4343, "socket": child["socket"]})
            self.assertTrue(parent["own_daemon"] and child["own_daemon"])

    def test_the_same_vm_reuses_its_own_daemon(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            mount = vf.mount_for(_ref(share_id="a" * 64), base, exported=True)
            spawned = []
            kwargs = dict(
                base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=lambda cmd, log_path: spawned.append(cmd) or 4242,
                pid_alive_fn=lambda pid: True,
            )
            first = vf.ensure_share_backend(mount, "vm-1", **kwargs)
            Path(first["socket"]).write_text("")
            again = vf.ensure_share_backend(mount, "vm-1", **kwargs)
            self.assertEqual(len(spawned), 1)
            self.assertEqual(again["pid"], 4242)

    def test_a_dead_daemon_of_this_vm_is_replaced(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            mount = vf.mount_for(_ref(share_id="a" * 64), base, exported=True)
            pids = iter([10, 11])
            kwargs = dict(
                base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=lambda cmd, log_path: next(pids),
                pid_alive_fn=lambda pid: False,
            )
            vf.ensure_share_backend(mount, "vm-1", **kwargs)
            again = vf.ensure_share_backend(mount, "vm-1", **kwargs)
            self.assertEqual(again["pid"], 11)
            self.assertEqual(vf.load_share_state(base, "a" * 64)["daemons"]["vm-1"]["pid"], 11)

    def test_a_share_that_fails_to_come_up_releases_the_ones_before_it(self):
        # The caller never gets their state back, so nothing else would stop these
        # daemons or drop this VM from the shares it had already reserved.
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            first, second = "a" * 64, "b" * 64
            mounts = [vf.mount_for(_ref(share_id=sid), base, exported=False)
                      for sid in (first, second)]
            # The parent exports both, so the shares outlive this VM's failure.
            for sid in (first, second):
                vf.reserve_share(base, vf.mount_for(_ref(share_id=sid), base, exported=True),
                                 "vm-parent")

            def spawn(cmd, log_path):
                if second[:12] in cmd[cmd.index("--socket-path") + 1]:
                    raise OSError("virtiofsd failed to start")
                return 4242

            killed = []
            with self.assertRaises(OSError):
                vf.attach_virtiofs_backends(
                    mounts, "vm-child", base_dir=base, socket_dir=sock,
                    virtiofsd_binary="virtiofsd", spawn_fn=spawn,
                    pid_alive_fn=lambda pid: True, kill_fn=killed.append,
                )
            self.assertEqual(killed, [4242])
            for sid in (first, second):
                state = vf.load_share_state(base, sid)
                self.assertEqual(state["users"], ["vm-parent"], sid)
                self.assertNotIn("vm-child", state.get("daemons") or {}, sid)

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
    PIDS = {"vm-parent": 101, "vm-child": 202}

    def _materialize(self, base, sid, *users):
        """Reserve the share for each user and record a daemon of its own, as
        ensure_share_backend does."""
        for vmachine_id, exported in users:
            mount = vf.mount_for(_ref(share_id=sid), base, exported=exported)
            vf.shared_dir(base, sid).mkdir(parents=True, exist_ok=True)
            vf.reserve_share(base, mount, vmachine_id)
        path = vf.share_state_path(base, sid)
        state = vf.load_share_state(base, sid)
        state["daemons"] = {
            vm: {"pid": self.PIDS[vm], "socket": str(Path(base) / f"{vm}.sock")}
            for vm, _ in users
        }
        vf._save_share_state(path, state)

    def test_a_guest_leaving_stops_only_its_own_daemon(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "b" * 64
            self._materialize(base, sid, ("vm-parent", True), ("vm-child", False))
            killed = []
            vf.teardown_virtiofs_for_vm(
                "vm-child", [{"share_id_hex": sid}], base_dir=base, kill_fn=killed.append,
            )
            self.assertEqual(killed, [202])
            self.assertTrue(vf.share_state_dir(base, sid).exists())
            state = vf.load_share_state(base, sid)
            self.assertEqual(list(state["daemons"]), ["vm-parent"])
            self.assertNotIn("released", state)

    def test_the_exporter_leaving_takes_every_daemon_and_the_data(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "c" * 64
            self._materialize(base, sid, ("vm-parent", True), ("vm-child", False))
            killed, logged = [], []
            vf.teardown_virtiofs_for_vm(
                "vm-parent", [{"share_id_hex": sid}], base_dir=base,
                kill_fn=killed.append, logger_fn=logged.append,
            )
            # Its own daemon, then the one of the guest that loses the directory.
            self.assertEqual(killed, [101, 202])
            self.assertFalse(vf.share_state_dir(base, sid).exists())
            # The guests that lose it are named, not left to be inferred.
            self.assertTrue(any("still running" in line for line in logged))

    def test_a_stopped_daemon_leaves_neither_its_socket_nor_its_pid_file(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "b" * 64
            self._materialize(base, sid, ("vm-parent", True), ("vm-child", False))
            socket = Path(base) / "vm-child.sock"
            socket.write_text("")
            Path(f"{socket}.pid").write_text("202")   # virtiofsd's own lock file
            vf.teardown_virtiofs_for_vm(
                "vm-child", [{"share_id_hex": sid}], base_dir=base, kill_fn=lambda pid: None,
            )
            self.assertFalse(socket.exists())
            self.assertFalse(Path(f"{socket}.pid").exists())

    def test_a_vm_whose_share_state_is_gone_still_stops_its_own_daemon(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "d" * 64
            killed = []
            vf.teardown_virtiofs_for_vm(
                "vm-child",
                [{"share_id_hex": sid, "pid": 303, "socket": str(Path(base) / "c.sock"),
                  "own_daemon": True}],
                base_dir=base, kill_fn=killed.append,
            )
            self.assertEqual(killed, [303])

    def test_a_share_from_an_older_node_keeps_its_single_daemon_for_the_exporter(self):
        # Before per-guest daemons, one daemon served everyone and was recorded at the
        # top level. A guest leaving must not stop it; the exporter leaving does.
        with tempfile.TemporaryDirectory() as base:
            sid = "e" * 64
            for vmachine_id, exported in (("vm-parent", True), ("vm-child", False)):
                vf.shared_dir(base, sid).mkdir(parents=True, exist_ok=True)
                vf.reserve_share(base, vf.mount_for(_ref(share_id=sid), base, exported=exported),
                                 vmachine_id)
            path = vf.share_state_path(base, sid)
            state = vf.load_share_state(base, sid)
            state.update({"pid": 999, "socket": str(Path(base) / "s.sock")})
            vf._save_share_state(path, state)
            # The guest's mount points at that shared daemon, without `own_daemon`.
            legacy_mount = {"share_id_hex": sid, "pid": 999, "socket": state["socket"]}

            killed = []
            vf.teardown_virtiofs_for_vm("vm-child", [legacy_mount], base_dir=base,
                                        kill_fn=killed.append)
            self.assertEqual(killed, [])
            vf.teardown_virtiofs_for_vm("vm-parent", [legacy_mount], base_dir=base,
                                        kill_fn=killed.append)
            self.assertEqual(killed, [999])
            self.assertFalse(vf.share_state_dir(base, sid).exists())

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
