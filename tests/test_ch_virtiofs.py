import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

# Load the virtiofs module directly from its file. Importing it via
# ``src.virtualizers.ch`` would run the package __init__, which eagerly imports
# the full Docker-backed CH runtime — unrelated to the pure virtiofs helpers
# under test. The module only depends on protos + src.utils.networks.
IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut

    _VFS_PATH = Path(__file__).resolve().parents[1] / "src" / "virtualizers" / "ch" / "virtiofs.py"
    _spec = importlib.util.spec_from_file_location("_ch_virtiofs_under_test", _VFS_PATH)
    virtiofs = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(virtiofs)
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    virtiofs = None  # type: ignore[assignment]


ABCD = b"ABCD-anchor-blob"


def _virtiofs_network(anchor=ABCD, tags=("shared-disk",), protocol_tags=("virtiofs",)):
    return celaut.Service.Network(
        tags=list(tags),
        formal=anchor,
        protocol_stack=[celaut.Service.Api.Protocol(tags=list(protocol_tags))],
    )


def _service(networks_list):
    return celaut.Service(network=list(networks_list))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PureBuilderTests(unittest.TestCase):
    def test_fs_device_arg_shape(self):
        arg = virtiofs.build_fs_device_arg("vfs-abc", "/tmp/s.sock")
        self.assertIn("tag=vfs-abc", arg)
        self.assertIn("socket=/tmp/s.sock", arg)
        self.assertIn("num_queues=", arg)
        self.assertIn("queue_size=", arg)

    def test_virtiofsd_command_confines_and_exports(self):
        cmd = virtiofs.build_virtiofsd_command("virtiofsd", "/tmp/s.sock", "/data/net", sandbox="chroot")
        self.assertEqual(cmd[0], "virtiofsd")
        self.assertIn("--socket-path", cmd)
        self.assertIn("/tmp/s.sock", cmd)
        self.assertIn("--shared-dir", cmd)
        self.assertIn("/data/net", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("chroot", cmd)

    def test_tag_is_short_and_stable(self):
        h = "a" * 64
        self.assertEqual(virtiofs.virtiofs_tag(h), virtiofs.virtiofs_tag(h))
        self.assertTrue(len(virtiofs.virtiofs_tag(h)) <= 20)

    def test_mounts_for_service_only_virtiofs(self):
        svc = _service([
            _virtiofs_network(anchor=ABCD),
            celaut.Service.Network(protocol_stack=[celaut.Service.Api.Protocol(tags=["http"])]),
        ])
        mounts = virtiofs.virtiofs_mounts_for_service(svc)
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0].anchor, ABCD)
        self.assertFalse(mounts[0].readonly)

    def test_mounts_carry_readonly_flag(self):
        svc = _service([_virtiofs_network(tags=("shared-disk", "ro"))])
        [m] = virtiofs.virtiofs_mounts_for_service(svc)
        self.assertTrue(m.readonly)

    def test_mounts_dedup_rw_wins_over_ro(self):
        # Same network declared ro and rw -> single mount, read-write (permissive).
        svc = _service([
            _virtiofs_network(anchor=ABCD, tags=("shared-disk", "ro")),
            _virtiofs_network(anchor=ABCD, tags=("shared-disk",)),
        ])
        mounts = virtiofs.virtiofs_mounts_for_service(svc)
        self.assertEqual(len(mounts), 1)
        self.assertFalse(mounts[0].readonly)

    def test_guest_mount_plan_json(self):
        svc = _service([_virtiofs_network(tags=("shared-disk", "readonly"))])
        mounts = virtiofs.virtiofs_mounts_for_service(svc)
        plan = json.loads(virtiofs.build_guest_mount_plan(mounts))
        self.assertEqual(len(plan), 1)
        self.assertIn("tag", plan[0])
        self.assertIn("path", plan[0])
        self.assertTrue(plan[0]["ro"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReferenceCountTests(unittest.TestCase):
    def test_network_used_by_other_vm(self):
        states = {
            "vm1": {"virtiofs": [{"network_id_hex": "aa"}]},
            "vm2": {"virtiofs": [{"network_id_hex": "bb"}]},
        }
        self.assertTrue(virtiofs.network_used_by_other_vm("aa", "vm2", states))
        self.assertFalse(virtiofs.network_used_by_other_vm("aa", "vm1", states))
        self.assertFalse(virtiofs.network_used_by_other_vm("cc", "vm1", states))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = str(Path(self._tmp.name) / "virtiofs")
        self.sock = str(Path(self._tmp.name) / "sock")
        self.spawned = []

    def tearDown(self):
        self._tmp.cleanup()

    def _spawn(self, command, log_path):
        self.spawned.append((command, log_path))
        return 4321  # fake pid

    def test_attach_starts_daemon_and_places_anchor(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        args, state, mounts = virtiofs.attach_virtiofs_backends(
            svc,
            base_dir=self.base,
            socket_dir=self.sock,
            virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn,
            pid_alive_fn=lambda _pid: False,
        )
        self.assertEqual(args[0], "--fs")
        self.assertEqual(len(state), 1)
        self.assertEqual(len(self.spawned), 1)  # daemon started once
        nid = mounts[0].network_id_hex
        anchor = virtiofs.shared_dir(self.base, nid) / virtiofs.ANCHOR_FILENAME
        self.assertTrue(anchor.is_file())
        self.assertEqual(anchor.read_bytes(), ABCD)
        # Shared dir confined to 0700.
        self.assertEqual(oct(virtiofs.shared_dir(self.base, nid).stat().st_mode & 0o777), "0o700")

    def test_daemon_reused_when_alive(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        nid = virtiofs.virtiofs_mounts_for_service(svc)[0].network_id_hex
        # First attach starts it.
        virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        # Create the socket file so the reuse check passes.
        virtiofs.virtiofs_socket_path(self.sock, nid).parent.mkdir(parents=True, exist_ok=True)
        virtiofs.virtiofs_socket_path(self.sock, nid).write_bytes(b"")
        # Second attach with a live pid must reuse (no new spawn).
        virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: True,
        )
        self.assertEqual(len(self.spawned), 1)

    def test_no_virtiofs_networks_is_noop(self):
        svc = _service([
            celaut.Service.Network(protocol_stack=[celaut.Service.Api.Protocol(tags=["http"])]),
        ])
        args, state, mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        self.assertEqual(args, [])
        self.assertEqual(state, [])
        self.assertEqual(mounts, [])
        self.assertEqual(self.spawned, [])

    def test_teardown_stops_daemon_when_last_user(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        _args, state, _mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        killed = []
        virtiofs.teardown_virtiofs_for_vm(
            "vm1", state, {},  # no other VMs
            base_dir=self.base,
            kill_fn=lambda pid: killed.append(pid),
        )
        self.assertEqual(killed, [4321])
        nid = state[0]["network_id_hex"]
        self.assertFalse(virtiofs.daemon_state_path(self.base, nid).exists())

    def test_teardown_keeps_daemon_when_other_vm_uses_it(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        _args, state, _mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        nid = state[0]["network_id_hex"]
        other = {"vm2": {"virtiofs": [{"network_id_hex": nid}]}}
        killed = []
        virtiofs.teardown_virtiofs_for_vm(
            "vm1", state, other,
            base_dir=self.base,
            kill_fn=lambda pid: killed.append(pid),
        )
        self.assertEqual(killed, [])  # still in use
        self.assertTrue(virtiofs.daemon_state_path(self.base, nid).exists())
        # Data preserved regardless.
        self.assertTrue((virtiofs.shared_dir(self.base, nid) / virtiofs.ANCHOR_FILENAME).is_file())

    def test_teardown_deletes_disk_on_last_by_default(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        _args, state, _mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        nid = state[0]["network_id_hex"]
        self.assertTrue(virtiofs.shared_dir(self.base, nid).is_dir())
        virtiofs.teardown_virtiofs_for_vm(
            "vm1", state, {},  # last user
            base_dir=self.base,
            kill_fn=lambda _pid: None,
        )
        # Last instance gone -> shared disk removed from the server by default.
        self.assertFalse(virtiofs.network_state_dir(self.base, nid).exists())
        self.assertFalse(virtiofs.shared_dir(self.base, nid).exists())

    def test_on_first_create_fires_once_on_create_not_on_join(self):
        # Origin recording hook: fires exactly once when the shared disk is first
        # created, and NOT when a later instance joins the already-created disk.
        svc = _service([_virtiofs_network(anchor=ABCD)])
        created = []
        # First attach creates the disk -> callback fires.
        virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
            on_first_create=created.append,
        )
        nid = virtiofs.virtiofs_mounts_for_service(svc)[0].network_id_hex
        self.assertEqual(created, [nid])
        # Make the daemon look alive + socket present so the second attach joins.
        virtiofs.virtiofs_socket_path(self.sock, nid).parent.mkdir(parents=True, exist_ok=True)
        virtiofs.virtiofs_socket_path(self.sock, nid).write_bytes(b"")
        # Second attach: disk already exists -> origin NOT overwritten (no fire).
        virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: True,
            on_first_create=created.append,
        )
        self.assertEqual(created, [nid])  # still only one origin record

    def test_shared_dir_usage_measures_du(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        _args, state, _mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        nid = state[0]["network_id_hex"]
        # Empty shared dir (only the anchor was written) -> anchor bytes counted.
        anchor_len = len(ABCD)
        self.assertEqual(virtiofs.shared_dir_usage_bytes(self.base, nid), anchor_len)
        # Write a payload file into the shared dir and re-measure.
        (virtiofs.shared_dir(self.base, nid) / "payload.bin").write_bytes(b"x" * 1000)
        self.assertEqual(virtiofs.shared_dir_usage_bytes(self.base, nid), anchor_len + 1000)

    def test_shared_dir_usage_missing_dir_is_zero(self):
        self.assertEqual(virtiofs.shared_dir_usage_bytes(self.base, "deadbeef"), 0)

    def test_attributed_usage_sums_networks(self):
        (virtiofs.shared_dir(self.base, "aa")).mkdir(parents=True, exist_ok=True)
        (virtiofs.shared_dir(self.base, "aa") / "f").write_bytes(b"x" * 100)
        (virtiofs.shared_dir(self.base, "bb")).mkdir(parents=True, exist_ok=True)
        (virtiofs.shared_dir(self.base, "bb") / "f").write_bytes(b"x" * 250)
        self.assertEqual(
            virtiofs.attributed_shared_disk_usage_bytes(
                ["aa", "bb"], base_dir=self.base, declared_disk_space=None
            ),
            350,
        )

    def test_attributed_usage_capped_by_declared(self):
        (virtiofs.shared_dir(self.base, "aa")).mkdir(parents=True, exist_ok=True)
        (virtiofs.shared_dir(self.base, "aa") / "f").write_bytes(b"x" * 1000)
        # Measured 1000 but declared cap 400 -> billed 400 (hard ceiling).
        self.assertEqual(
            virtiofs.attributed_shared_disk_usage_bytes(
                ["aa"], base_dir=self.base, declared_disk_space=400
            ),
            400,
        )
        # Under the cap -> measured returned.
        self.assertEqual(
            virtiofs.attributed_shared_disk_usage_bytes(
                ["aa"], base_dir=self.base, declared_disk_space=5000
            ),
            1000,
        )

    def test_attributed_usage_zero_declared_means_no_cap(self):
        (virtiofs.shared_dir(self.base, "aa")).mkdir(parents=True, exist_ok=True)
        (virtiofs.shared_dir(self.base, "aa") / "f").write_bytes(b"x" * 300)
        self.assertEqual(
            virtiofs.attributed_shared_disk_usage_bytes(
                ["aa"], base_dir=self.base, declared_disk_space=0
            ),
            300,
        )

    def test_on_disk_deleted_fires_on_last_teardown(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        _args, state, _mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        nid = state[0]["network_id_hex"]
        forgotten = []
        virtiofs.teardown_virtiofs_for_vm(
            "vm1", state, {},  # last user -> disk deleted
            base_dir=self.base,
            kill_fn=lambda _pid: None,
            on_disk_deleted=forgotten.append,
        )
        self.assertEqual(forgotten, [nid])

    def test_on_disk_deleted_not_fired_when_other_vm_uses_network(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        _args, state, _mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        nid = state[0]["network_id_hex"]
        other = {"vm2": {"virtiofs": [{"network_id_hex": nid}]}}
        forgotten = []
        virtiofs.teardown_virtiofs_for_vm(
            "vm1", state, other,  # still in use -> disk kept, origin kept
            base_dir=self.base,
            kill_fn=lambda _pid: None,
            on_disk_deleted=forgotten.append,
        )
        self.assertEqual(forgotten, [])

    def test_teardown_preserves_disk_when_flag_false(self):
        svc = _service([_virtiofs_network(anchor=ABCD)])
        _args, state, _mounts = virtiofs.attach_virtiofs_backends(
            svc, base_dir=self.base, socket_dir=self.sock, virtiofsd_binary="virtiofsd",
            spawn_fn=self._spawn, pid_alive_fn=lambda _pid: False,
        )
        nid = state[0]["network_id_hex"]
        killed = []
        virtiofs.teardown_virtiofs_for_vm(
            "vm1", state, {},  # last user
            base_dir=self.base,
            delete_disk_on_last=False,
            kill_fn=lambda pid: killed.append(pid),
        )
        self.assertEqual(killed, [4321])  # daemon still stopped
        # ...but the shared disk (data + anchor) is kept for future reuse.
        self.assertTrue(virtiofs.shared_dir(self.base, nid).is_dir())
        self.assertTrue((virtiofs.shared_dir(self.base, nid) / virtiofs.ANCHOR_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
