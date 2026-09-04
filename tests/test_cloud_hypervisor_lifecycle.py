"""Teardown, health check and janitor for a microVM, over the shared implementation.

One ``kill`` and one ``maintain`` serve the whole family, pointed at the member
that booted the guest by its ``Hypervisor`` descriptor. What used to be two
near-identical copies per operation is one, so these tests exercise the CH member
and the QEMU one through the same code.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.microvm import kill as microvm_kill
    from src.virtualizers.microvm import maintain as microvm_maintain
    from src.virtualizers.microvm import members
    from src.virtualizers.microvm import process as microvm_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    microvm_kill = None  # type: ignore[assignment]
    microvm_maintain = None  # type: ignore[assignment]
    members = None  # type: ignore[assignment]
    microvm_process = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MicroVMLifecycleTests(unittest.TestCase):
    def test_kill_cleans_runtime_resources_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime-vm"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            socket_dir = Path(tmpdir) / "sockets"
            socket_dir.mkdir(parents=True, exist_ok=True)
            socket_path = socket_dir / "ch-vm-1.sock"
            socket_path.touch()
            state = {
                "pid": 12345,
                "tap": "tapabc",
                "cgroup_path": "/sys/fs/cgroup/nodo-ch/vm-1",
                "process_name": members.CH.process_name("vm-1"),
                "control_socket": str(socket_path),
                "dnat_rules": [
                    {
                        "protocol": "tcp",
                        "external_port": 40000,
                        "internal_port": 8080,
                        "destination_ip": "192.168.200.10",
                    }
                ],
            }
            self.assertTrue(runtime_dir.exists())
            self.assertTrue(socket_path.exists())

            with patch.object(microvm_kill, "load_runtime_state", return_value=state), patch.object(
                microvm_kill, "_runtime_dir", return_value=runtime_dir
            ), patch.object(
                microvm_kill, "pid_matches", return_value=True
            ), patch.object(
                microvm_kill.os, "kill", side_effect=ProcessLookupError
            ) as os_kill, patch.object(
                microvm_kill, "remove_vm_cgroup"
            ) as remove_vm_cgroup, patch.object(
                microvm_kill, "delete_runtime_state"
            ) as delete_runtime_state:
                result = microvm_kill.kill(members.CH, vmachine_id="vm-1")

        self.assertTrue(result)
        os_kill.assert_called_once()
        remove_vm_cgroup.assert_called_once_with(
            vmachine_id="vm-1",
            cgroup_path="/sys/fs/cgroup/nodo-ch/vm-1",
        )
        delete_runtime_state.assert_called_once_with("vm-1")
        self.assertFalse(runtime_dir.exists())
        self.assertFalse(socket_path.exists())

    def test_kill_does_not_signal_reused_pid(self):
        state = {"pid": 12345, "tap": "", "control_socket": "", "cleanup_rules": []}
        with patch.object(microvm_kill, "load_runtime_state", return_value=state), patch.object(
            microvm_kill, "pid_matches", return_value=False
        ), patch.object(
            microvm_kill.os, "kill"
        ) as os_kill, patch.object(
            microvm_kill, "remove_vm_cgroup"
        ), patch.object(
            microvm_kill, "delete_runtime_state"
        ):
            result = microvm_kill.kill(members.CH, vmachine_id="vm-reused")

        self.assertTrue(result)
        os_kill.assert_not_called()

    def test_kill_matches_the_recorded_name_not_the_members_own(self):
        """A guest recorded by one member is never matched by another's naming.

        The whole point of recording the name: ``kill`` derives it only when the
        entry carries none.
        """
        state = {
            "pid": 12345,
            "process_name": members.QEMU.process_name("vm-q"),
            "control_socket": "",
        }
        with patch.object(microvm_kill, "load_runtime_state", return_value=state), patch.object(
            microvm_kill, "pid_matches", return_value=True
        ) as pid_matches, patch.object(
            microvm_kill.os, "kill", side_effect=ProcessLookupError
        ), patch.object(
            microvm_kill, "remove_vm_cgroup"
        ), patch.object(
            microvm_kill, "delete_runtime_state"
        ):
            microvm_kill.kill(members.QEMU, vmachine_id="vm-q")

        pid_matches.assert_called_once_with(
            pid=12345, process_name=members.QEMU.process_name("vm-q")
        )

    def test_maintain_penalizes_when_state_or_pid_invalid(self):
        removed = []

        def _remove(vmachine_id):
            removed.append(vmachine_id)

        with patch.object(microvm_maintain, "load_runtime_state", return_value=None):
            microvm_maintain.maintain(
                members.CH,
                vmachine_id="vm-missing",
                debug_mode=True,
                remove_and_penalize=_remove,
            )
        self.assertEqual(removed, ["vm-missing"])

        removed.clear()
        with patch.object(microvm_maintain, "load_runtime_state", return_value={"pid": 0}):
            microvm_maintain.maintain(
                members.CH,
                vmachine_id="vm-invalid-pid",
                debug_mode=True,
                remove_and_penalize=_remove,
            )
        self.assertEqual(removed, ["vm-invalid-pid"])

    def test_maintain_penalizes_when_the_control_socket_is_gone(self):
        removed = []

        def _remove(vmachine_id):
            removed.append(vmachine_id)

        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 222, "control_socket": "/tmp/not-found.sock"},
        ), patch.object(
            microvm_maintain, "_state_is_alive", return_value=True
        ):
            microvm_maintain.maintain(
                members.CH,
                vmachine_id="vm-socket",
                debug_mode=True,
                remove_and_penalize=_remove,
            )

        self.assertEqual(removed, ["vm-socket"])

    def test_maintain_checks_a_qemu_guests_socket_too(self):
        """The check is the family's, so it covers whatever member recorded one.

        QEMU's control socket is its QMP socket. An emulator that has dropped it
        is as gone as a cloud-hypervisor that dropped its API socket, and used to
        be reported healthy because only CH's copy of ``maintain`` looked.
        """
        removed = []
        with patch.object(
            microvm_maintain,
            "load_runtime_state",
            return_value={"pid": 333, "control_socket": "/tmp/not-found-qmp.sock"},
        ), patch.object(
            microvm_maintain, "_state_is_alive", return_value=True
        ):
            microvm_maintain.maintain(
                members.QEMU,
                vmachine_id="vm-q",
                debug_mode=True,
                remove_and_penalize=lambda vmachine_id: removed.append(vmachine_id),
            )

        self.assertEqual(removed, ["vm-q"])

    def test_pid_alive_rejects_zombie_process(self):
        with patch.object(microvm_process.os, "kill", return_value=None), patch.object(
            microvm_process, "proc_state", return_value="Z"
        ), patch.object(
            microvm_process, "pid_matches", return_value=True
        ) as pid_matches:
            alive = microvm_process.pid_alive(pid=222, process_name="nodo-ch-vmzombie")

        self.assertFalse(alive)
        pid_matches.assert_not_called()

    def test_pid_alive_rejects_reused_pid_for_another_process(self):
        with patch.object(microvm_process.os, "kill", return_value=None), patch.object(
            microvm_process, "proc_state", return_value="S"
        ), patch.object(
            microvm_process, "pid_matches", return_value=False
        ):
            alive = microvm_process.pid_alive(pid=222, process_name="nodo-ch-vmreused")

        self.assertFalse(alive)

    def _sweep(self, states, *, in_db, alive=True):
        with patch.object(
            microvm_maintain, "list_runtime_states", return_value=states
        ), patch.object(
            microvm_maintain.sc, "internal_instance_exists", return_value=in_db
        ), patch.object(
            microvm_maintain, "_state_is_alive", return_value=alive
        ), patch.object(
            microvm_maintain, "kill_vm", return_value=True
        ) as kill_mock:
            microvm_maintain.sweep_orphans(debug_mode=False)
        return kill_mock

    def test_janitor_cleans_orphan_runtime(self):
        kill_mock = self._sweep(
            {"vm-orphan": {"pid": 777, "virtualizer": "ch"}}, in_db=False
        )
        kill_mock.assert_called_once_with(members.CH, vmachine_id="vm-orphan")

    def test_janitor_cleans_stale_dead_process(self):
        kill_mock = self._sweep(
            {"vm-dead": {"pid": 888, "virtualizer": "ch"}}, in_db=True, alive=False
        )
        kill_mock.assert_called_once_with(members.CH, vmachine_id="vm-dead")

    def test_janitor_skips_healthy_registered_runtime(self):
        kill_mock = self._sweep({"vm-ok": {"pid": 999, "virtualizer": "ch"}}, in_db=True)
        kill_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
