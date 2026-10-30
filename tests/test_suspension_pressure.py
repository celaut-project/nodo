"""Suspended instances that nobody pays for cannot fill the host (F4 of the review of #595).

An instance out of balance keeps its disk and its address for the grace period, for
free. A client could launch many with a deposit of about one tick and have them all
suspended at once: 253 of them took every address of the guest subnet. Two things stop
that. A father may keep only so many of them, and only so much disk in them. And a
launch that finds no disk or no address deletes the oldest of them before it is refused.
"""

import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.gateway.launcher.local_execution import local_execution
    from src.manager import maintain
    from src.utils import dead_instances
    from src.utils.cost_functions import resource_availability
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

GIB = 1024 ** 3


def _row(token, father="f", disk=GIB, reason=None, balance=0):
    return {"id": token, "father_id": father, "disk_space": disk, "balance_mu": balance,
            "suspended_reason": reason or dead_instances.OUT_OF_BALANCE}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CapsPerFatherTests(unittest.TestCase):
    def _enforce(self, rows, count=10, disk_gib=20, can_pay=False):
        stopped = []
        with mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "_can_pay_a_tick", return_value=can_pay), \
                mock.patch.object(maintain, "system_scarcity", return_value={}), \
                mock.patch.object(dead_instances, "max_suspended_per_father", return_value=count), \
                mock.patch.object(dead_instances, "max_suspended_disk_bytes_per_father",
                                  return_value=int(disk_gib * GIB)), \
                mock.patch.object(maintain, "stop_instance",
                                  side_effect=lambda token, reason: stopped.append((token, reason)) or 0):
            deleted = maintain.enforce_suspension_caps("f")
        return deleted, stopped

    def test_over_the_count_the_oldest_go(self):
        rows = [_row(f"i{n}") for n in range(12)]
        deleted, stopped = self._enforce(rows, count=10, disk_gib=0)
        self.assertEqual(deleted, 2)
        self.assertEqual(stopped, [("i0", dead_instances.OUT_OF_BALANCE),
                                   ("i1", dead_instances.OUT_OF_BALANCE)])

    def test_over_the_disk_the_oldest_go_until_it_fits(self):
        rows = [_row("big", disk=15 * GIB), _row("mid", disk=6 * GIB), _row("new", disk=3 * GIB)]
        deleted, stopped = self._enforce(rows, count=0, disk_gib=20)
        self.assertEqual([token for token, _ in stopped], ["big"])

    def test_under_both_nothing_goes(self):
        self.assertEqual(self._enforce([_row("a"), _row("b")]), (0, []))

    def test_other_fathers_and_instances_with_mu_do_not_count(self):
        rows = [_row("other", father="g"), _row("lost", reason=dead_instances.PROCESS_LOST),
                _row("a"), _row("b")]
        self.assertEqual(self._enforce(rows, count=1, disk_gib=0)[1],
                         [("a", dead_instances.OUT_OF_BALANCE)])

    def test_one_that_was_topped_up_is_not_counted(self):
        self.assertEqual(self._enforce([_row("a", balance=10 ** 9), _row("b", balance=10 ** 9)],
                                       count=1, can_pay=True), (0, []))

    def test_zero_turns_both_caps_off(self):
        self.assertEqual(self._enforce([_row(f"i{n}") for n in range(50)], count=0, disk_gib=0), (0, []))

    def test_the_tick_holds_the_father_to_the_caps_when_it_suspends(self):
        with mock.patch.object(maintain.sc, "get_running_internal_containers_ids", return_value=["vm-1"]), \
                mock.patch.object(maintain.sc, "get_all_internal_containers_ids", return_value=["vm-1"]), \
                mock.patch.object(maintain.sc, "get_sys_req", return_value={
                    "mem_limit": GIB, "disk_space": GIB, "cpu_period": 100000, "cpu_quota": 100000}), \
                mock.patch.object(maintain.sc, "get_internal_father_id", return_value="f"), \
                mock.patch.object(maintain, "vm_maintain"), \
                mock.patch.object(maintain, "spend_mu", return_value=False), \
                mock.patch.object(maintain, "suspend_instance", return_value=True), \
                mock.patch.object(maintain.dead_instances, "suspended_grace_seconds", return_value=3600), \
                mock.patch.object(maintain, "system_scarcity", return_value={"cpu": 0, "mem": 0, "disk": 0}), \
                mock.patch.object(maintain, "enforce_suspension_caps") as enforce:
            maintain.maintain_vmachines()
        enforce.assert_called_once_with("f")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PressureTests(unittest.TestCase):
    def test_a_launch_short_of_disk_alone_frees_it_and_is_admitted(self):
        # The first answer refuses; then one before each deletion, until it fits; then
        # the answer the launch gets.
        answers = iter([
            {"can_execute": False, "reason": "Disk ceiling reached (host_limits.MAX_DISK_SHARE). x"},
            {"can_execute": False, "reason": "Disk ceiling reached (host_limits.MAX_DISK_SHARE). x"},
            {"can_execute": False, "reason": "Insufficient disk for resources.at_most.disk_space. x"},
            {"can_execute": True, "reason": ""},
            {"can_execute": True, "reason": ""},
        ])
        stopped = []
        with mock.patch.object(resource_availability, "get_resource_availability",
                               side_effect=lambda **_: next(answers)), \
                mock.patch.object(maintain.sc, "get_suspended_local_instances",
                                  return_value=[_row("a"), _row("b"), _row("c")]), \
                mock.patch.object(maintain, "stop_instance",
                                  side_effect=lambda token, reason: stopped.append(token) or 0):
            availability = resource_availability.availability_reclaiming_disk(resources=None)
        self.assertTrue(availability["can_execute"])
        self.assertEqual(stopped, ["a", "b"])

    def test_a_launch_short_of_something_else_too_deletes_nothing(self):
        refused = {"can_execute": False,
                   "reason": "Insufficient memory for x | Insufficient disk for resources.at_most.disk_space."}
        with mock.patch.object(resource_availability, "get_resource_availability", return_value=refused), \
                mock.patch.object(maintain, "stop_instance") as stop:
            self.assertFalse(resource_availability.availability_reclaiming_disk(resources=None)["can_execute"])
        stop.assert_not_called()

    def test_only_the_ones_out_of_balance_go(self):
        rows = [_row("lost", reason=dead_instances.PROCESS_LOST), _row("broke")]
        with mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "stop_instance", return_value=0) as stop:
            self.assertTrue(maintain.reclaim_suspended_address())
        stop.assert_called_once_with(token="broke", reason=dead_instances.OUT_OF_BALANCE)

    def test_no_free_address_is_recognised(self):
        self.assertTrue(local_execution._no_free_address(
            RuntimeError("No available IPs in subnet 192.168.200.0/24")))
        self.assertFalse(local_execution._no_free_address(RuntimeError("boot timed out")))


if __name__ == "__main__":
    unittest.main()
