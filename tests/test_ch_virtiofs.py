"""Unit tests for the Cloud Hypervisor shared-filesystem (virtiofs) backend.

The pure builders are tested directly; the daemon spawn/teardown orchestration is
dependency-injected so it runs on a host that cannot start microVMs.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

try:
    from protos import celaut_pb2 as celaut
    from src.utils import shared_filesystems as sf
    # Load the backend module directly from its file: importing it through the
    # `src.virtualizers.ch` package would run that package's __init__, which pulls
    # in the full CH build stack (bee_rpc etc.). The backend itself only depends on
    # protos + shared_filesystems, so it loads and tests standalone.
    _VF_PATH = Path(__file__).resolve().parents[1] / "src" / "virtualizers" / "ch" / "virtiofs.py"
    _spec = importlib.util.spec_from_file_location("ch_virtiofs_under_test", _VF_PATH)
    vf = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(vf)
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    sf = None
    vf = None


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
class VirtiofsBuildersTest(unittest.TestCase):
    def test_parent_export_and_child_guest_share_the_same_id(self):
        # The parent exports /mnt/db (share id = H(parent_id, /mnt/db)); a child
        # that declares /mnt/db as a guest dir reconstructs the SAME id from its
        # father_id, so it attaches to exactly the parent's export.
        parent_id = "parent-vm-1"
        parent = _service(_dir("mnt", children=[_dir("db", {"shared": b"true"})]))
        child = _service(_dir("mnt", children=[_dir("db", {"guest": b"true", "access": b"ro"})]))

        pmounts = vf.parent_export_mounts(parent, parent_id, base_dir="/base")
        cmounts = vf.child_guest_mounts(child, parent_id, base_dir="/base")

        self.assertEqual(len(pmounts), 1)
        self.assertEqual(len(cmounts), 1)
        self.assertEqual(pmounts[0].share_id_hex, cmounts[0].share_id_hex)
        self.assertEqual(pmounts[0].tag, cmounts[0].tag)
        self.assertFalse(pmounts[0].readonly)      # exporter mounts rw
        self.assertTrue(cmounts[0].readonly)       # child asked for ro
        self.assertEqual(cmounts[0].guest_path, "/mnt/db")

    def test_child_with_no_father_has_no_mounts(self):
        child = _service(_dir("data", {"guest": b"true"}))
        self.assertEqual(vf.child_guest_mounts(child, "", base_dir="/base"), [])

    def test_child_cannot_reach_a_different_parents_share(self):
        child = _service(_dir("data", {"guest": b"true"}))
        a = vf.child_guest_mounts(child, "parentA", base_dir="/base")[0]
        b = vf.child_guest_mounts(child, "parentB", base_dir="/base")[0]
        self.assertNotEqual(a.share_id_hex, b.share_id_hex)

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

    def test_reference_count_ignores_self(self):
        states = {
            "vm-self": {"virtiofs": [{"share_id_hex": "S"}]},
            "vm-other": {"virtiofs": [{"share_id_hex": "S"}]},
        }
        self.assertTrue(vf.share_used_by_other_vm("S", "vm-self", states))
        self.assertFalse(vf.share_used_by_other_vm("S", "vm-self", {"vm-self": states["vm-self"]}))


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class VirtiofsOrchestrationTest(unittest.TestCase):
    def test_attach_empty_is_a_noop(self):
        args, state, mounts = vf.attach_virtiofs_backends(
            [], base_dir="/base", socket_dir="/sock", virtiofsd_binary="virtiofsd",
        )
        self.assertEqual((args, state, mounts), ([], [], []))

    def test_ensure_backend_spawns_then_reuses(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            mount = vf.SharedMount(
                share_id_hex="a" * 64, tag="vfs-aaaa", readonly=False,
                guest_path="/mnt/db", host_dir="",
            )
            spawned = []

            def fake_spawn(cmd, log_path):
                spawned.append(cmd)
                return 4242

            state = vf.ensure_share_backend(
                mount, base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=fake_spawn, pid_alive_fn=lambda pid: True,
            )
            self.assertEqual(state["pid"], 4242)
            self.assertTrue(Path(vf.shared_dir(base, "a" * 64)).is_dir())
            self.assertEqual(len(spawned), 1)

            # Socket must exist for reuse; simulate the daemon having bound it.
            Path(state["socket"]).write_text("")
            vf.ensure_share_backend(
                mount, base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
                spawn_fn=fake_spawn, pid_alive_fn=lambda pid: True,
            )
            self.assertEqual(len(spawned), 1)  # reused, not spawned again

    def test_teardown_stops_daemon_and_preserves_parents_data_for_child(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "b" * 64
            # Materialize a share dir + daemon state as if a daemon were running.
            vf._save_daemon_state(
                vf.daemon_state_path(base, sid),
                {"share_id_hex": sid, "pid": 999, "socket": str(Path(base) / "s.sock")},
            )
            vf.shared_dir(base, sid).mkdir(parents=True, exist_ok=True)
            killed = []

            # Child detaches: NOT the owner -> daemon stops but data is preserved.
            vf.teardown_virtiofs_for_vm(
                vmachine_id="child-vm",
                mounts_state=[{"share_id_hex": sid, "pid": 999}],
                runtime_states={},              # no other VM uses it
                base_dir=base,
                owned_share_ids=[],             # child does not own the share
                kill_fn=lambda pid: killed.append(pid),
            )
            self.assertEqual(killed, [999])
            self.assertTrue(vf.share_state_dir(base, sid).exists())  # parent data kept

            # Now the owning parent departs -> exported data removed.
            vf._save_daemon_state(
                vf.daemon_state_path(base, sid),
                {"share_id_hex": sid, "pid": 1000, "socket": str(Path(base) / "s.sock")},
            )
            vf.teardown_virtiofs_for_vm(
                vmachine_id="parent-vm",
                mounts_state=[{"share_id_hex": sid, "pid": 1000}],
                runtime_states={},
                base_dir=base,
                owned_share_ids=[sid],          # parent owns the share
                kill_fn=lambda pid: killed.append(pid),
            )
            self.assertEqual(killed, [999, 1000])
            self.assertFalse(vf.share_state_dir(base, sid).exists())  # data removed

    def test_teardown_keeps_daemon_when_another_vm_still_uses_share(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "c" * 64
            vf._save_daemon_state(
                vf.daemon_state_path(base, sid),
                {"share_id_hex": sid, "pid": 5, "socket": str(Path(base) / "s.sock")},
            )
            vf.shared_dir(base, sid).mkdir(parents=True, exist_ok=True)
            killed = []
            vf.teardown_virtiofs_for_vm(
                vmachine_id="child-vm",
                mounts_state=[{"share_id_hex": sid, "pid": 5}],
                runtime_states={"other-vm": {"virtiofs": [{"share_id_hex": sid}]}},
                base_dir=base,
                owned_share_ids=[],
                kill_fn=lambda pid: killed.append(pid),
            )
            self.assertEqual(killed, [])  # daemon left running for the other VM
            self.assertTrue(vf.daemon_state_path(base, sid).exists())


if __name__ == "__main__":
    unittest.main()
