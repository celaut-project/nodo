"""A revive, a suspension and a stop of one instance never overlap (F2 of the review of #595).

A revive boots the guest, which takes seconds. A stop that landed in the middle used to
mark the row dead while the new VM went on running on a deleted disk, unbilled, with its
address free for the next launch.
"""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.manager import manager
    from src.utils import instance_lock
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.object(instance_lock, "lock_dir", return_value=Path(self.dir.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_thread_that_holds_it_can_take_it_again(self):
        with instance_lock.instance_lock("vm"):
            with instance_lock.instance_lock("vm"):
                pass

    def test_another_thread_waits_for_it(self):
        order = []
        held = threading.Event()

        def other():
            held.wait()
            with instance_lock.instance_lock("vm"):
                order.append("other")

        thread = threading.Thread(target=other)
        thread.start()
        with instance_lock.instance_lock("vm"):
            held.set()
            time.sleep(0.2)
            order.append("first")
        thread.join(5)
        self.assertEqual(order, ["first", "other"])

    def test_another_process_cannot_take_it(self):
        # What `nodo kill` is: another process, which sees the lock through the file.
        probe = (
            "import fcntl, sys\n"
            "f = open(sys.argv[1], 'a')\n"
            "try:\n"
            "    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print('took')\n"
            "except BlockingIOError:\n"
            "    print('busy')\n"
        )
        path = str(instance_lock.lock_path("vm"))
        with instance_lock.instance_lock("vm"):
            inside = subprocess.run([sys.executable, "-c", probe, path],
                                    capture_output=True, text=True).stdout.strip()
        after = subprocess.run([sys.executable, "-c", probe, path],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual((inside, after), ("busy", "took"))

    def test_forget_removes_the_file(self):
        with instance_lock.instance_lock("vm"):
            instance_lock.forget("vm")
        self.assertFalse(instance_lock.lock_path("vm").exists())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReviveAgainstStopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.events = []
        self.resumed = True
        service = celaut.Service()
        sys_req = {"mem_limit": 1, "disk_space": 1, "cpu_period": 1, "cpu_quota": 1, "arch": None}
        self.booting = threading.Event()

        def execute(**_kwargs):
            self.events.append("boot-start")
            self.booting.set()
            time.sleep(0.3)
            self.events.append("boot-end")
            return ("vm", "192.168.200.7", None)

        def stop(token, credit, reason):
            self.events.append("stop")
            return 0

        def kill(**kwargs):
            self.events.append(("kill", kwargs.get("keep_disk")))

        for patch in (
            mock.patch.object(instance_lock, "lock_dir", return_value=Path(self.dir.name)),
            mock.patch.object(manager.sc, "internal_instance_exists", return_value=True),
            mock.patch.object(manager.sc, "is_local_instance_suspended", return_value=True),
            mock.patch.object(manager.sc, "get_service_id_by_container_id", return_value="svc"),
            mock.patch.object(manager.sc, "get_internal_father_id", return_value="father"),
            mock.patch.object(manager.sc, "get_sys_req", return_value=sys_req),
            mock.patch.object(manager.sc, "get_internal_ip", return_value="192.168.200.7"),
            mock.patch.object(manager.sc, "resume_local_instance", side_effect=lambda _t: self.resumed),
            mock.patch("src.utils.utils.read_service_from_disk", return_value=service),
            mock.patch("src.utils.cost_functions.resource_availability.get_resource_availability",
                       return_value={"can_execute": True}),
            mock.patch.object(manager, "kill", side_effect=kill),
            mock.patch.object(manager, "vm_execute", side_effect=execute),
            mock.patch.object(manager, "_stop_instance", side_effect=stop),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_stop_during_the_boot_waits_for_it(self):
        reviver = threading.Thread(target=manager.revive_instance, args=("vm",))
        reviver.start()
        self.assertTrue(self.booting.wait(5))
        manager.stop_instance(token="vm")
        reviver.join(5)
        # The stop came after the boot, so it is the one that takes down the new VM.
        self.assertEqual([e for e in self.events if not isinstance(e, tuple)],
                         ["boot-start", "boot-end", "stop"])

    def test_a_row_that_is_no_longer_suspended_after_the_boot_has_its_new_vm_killed(self):
        self.resumed = False
        self.assertIs(manager.revive_instance("vm"), False)
        # Killed after the boot, not only the old VM before it.
        self.assertEqual(self.events[-1], ("kill", True))
        self.assertEqual(self.events.count("boot-end"), 1)
        self.assertLess(self.events.index("boot-end"), len(self.events) - 1)


if __name__ == "__main__":
    unittest.main()
