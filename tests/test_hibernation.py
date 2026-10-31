"""Setting CPU or RAM to 0 hibernates an instance, and giving both back wakes it (issue #498).

A hibernated instance is a suspended one with the reason `hibernated`: its VM is stopped,
its disk and row are kept, nothing but a resize brings it back, and it pays for its disk.
"""

import sqlite3
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.gateway import gateway
    from src.manager import hibernation, maintain
    from src.utils import dead_instances
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


def _sysreq(**fields):
    return celaut_pb2.Sysresources(**fields)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HibernationTestCase(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original_connection = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.sc.add_local_instance(
            father_id="father", container_ip="192.168.200.7", container_id="a",
            name="web", balance_mu=100, serialized_instance="x", service_id="svc",
            virtualizer="ch", mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50,
            envs="{}",
        )

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original_connection


class PlanTests(HibernationTestCase):
    def test_zero_cpu_or_zero_ram_on_a_running_instance_hibernates_it(self):
        self.assertEqual(hibernation.plan("a", _sysreq(mem_limit=0)), hibernation.HIBERNATE)
        self.assertEqual(hibernation.plan("a", _sysreq(cpu_quota=0)), hibernation.HIBERNATE)
        self.assertEqual(
            hibernation.plan("a", _sysreq(mem_limit=10, cpu_quota=0)), hibernation.HIBERNATE)

    def test_a_field_that_is_not_set_keeps_the_value_of_the_row(self):
        self.assertEqual(hibernation.plan("a", _sysreq(mem_limit=2000)), hibernation.RESIZE)
        self.assertEqual(hibernation.plan("a", _sysreq()), hibernation.RESIZE)

    def test_both_above_zero_on_a_hibernated_instance_wakes_it(self):
        self.sc.hibernate_local_instance("a")
        request = _sysreq(mem_limit=1000, cpu_period=100, cpu_quota=50)
        self.assertEqual(hibernation.plan("a", request), hibernation.WAKE)

    def test_only_one_of_them_does_not_wake_it(self):
        self.sc.hibernate_local_instance("a")
        self.assertEqual(hibernation.plan("a", _sysreq(mem_limit=1000)), hibernation.STAY)
        self.assertEqual(
            hibernation.plan("a", _sysreq(cpu_period=100, cpu_quota=50)), hibernation.STAY)


class RowTests(HibernationTestCase):
    def test_hibernating_keeps_the_row_and_the_disk_and_zeroes_cpu_and_ram(self):
        self.assertTrue(self.sc.hibernate_local_instance("a"))
        row = self.sc.get_sys_req(id="a")
        self.assertEqual(
            (row["mem_limit"], row["cpu_period"], row["cpu_quota"], row["disk_space"]),
            (0, 0, 0, 500))
        self.assertTrue(self.sc.internal_instance_exists("a"))
        self.assertTrue(self.sc.is_local_instance_hibernated("a"))
        self.assertEqual(self.sc.get_running_internal_containers_ids(), [])

    def test_hibernating_twice_changes_nothing(self):
        self.assertTrue(self.sc.hibernate_local_instance("a"))
        self.assertFalse(self.sc.hibernate_local_instance("a"))

    def test_one_suspended_for_lack_of_mu_becomes_hibernated_so_a_top_up_does_not_start_it(self):
        self.sc.suspend_local_instance("a", dead_instances.OUT_OF_BALANCE)
        self.assertTrue(self.sc.hibernate_local_instance("a"))
        self.assertTrue(self.sc.is_local_instance_hibernated("a"))

    def test_a_plain_suspension_is_not_a_hibernation(self):
        self.sc.suspend_local_instance("a")
        self.assertFalse(self.sc.is_local_instance_hibernated("a"))
        self.assertEqual(self.sc.get_hibernated_local_instances(), [])

    def test_a_hibernated_instance_holds_only_its_disk_on_the_host(self):
        self.sc.hibernate_local_instance("a")
        (held,) = self.sc.get_committed_resources()
        self.assertEqual((held["mem_limit"], held["cpu_quota"], held["disk_space"]), (0, 0, 500))


class HibernateTests(HibernationTestCase):
    def test_it_stops_the_vm_and_keeps_the_disk(self):
        with mock.patch.object(hibernation, "revivable", return_value=True), \
                mock.patch.object(hibernation, "_uses_shares", return_value=False), \
                mock.patch.object(hibernation, "kill") as kill:
            self.assertTrue(hibernation.hibernate_instance("a"))
        kill.assert_called_once_with(vmachine_id="a", archive=False, keep_disk=True)
        self.assertTrue(self.sc.is_local_instance_hibernated("a"))

    def test_a_vm_that_does_not_stop_leaves_the_row_as_it_was(self):
        with mock.patch.object(hibernation, "revivable", return_value=True), \
                mock.patch.object(hibernation, "_uses_shares", return_value=False), \
                mock.patch.object(hibernation, "kill", side_effect=RuntimeError("stuck")):
            with self.assertRaises(RuntimeError):
                hibernation.hibernate_instance("a")
        self.assertFalse(self.sc.is_local_instance_suspended("a"))
        self.assertEqual(self.sc.get_sys_req(id="a")["mem_limit"], 1000)

    def test_one_already_hibernated_is_left_alone(self):
        self.sc.hibernate_local_instance("a")
        with mock.patch.object(hibernation, "kill") as kill:
            self.assertTrue(hibernation.hibernate_instance("a"))
        kill.assert_not_called()

    def test_an_instance_that_cannot_be_started_again_is_refused(self):
        with mock.patch.object(hibernation, "revivable", return_value=False):
            with self.assertRaises(hibernation.HibernationRefused):
                hibernation.check_can_hibernate("a")

    def test_an_instance_with_a_shared_filesystem_is_refused_and_says_why(self):
        with mock.patch.object(hibernation, "revivable", return_value=True), \
                mock.patch.object(hibernation, "_uses_shares", return_value=True):
            with self.assertRaises(hibernation.HibernationRefused) as raised:
                hibernation.check_can_hibernate("a")
        self.assertIn("shared filesystem", str(raised.exception))

    def test_an_instance_that_is_gone_is_not_hibernated(self):
        self.assertFalse(hibernation.hibernate_instance("missing"))


class WakeTests(HibernationTestCase):
    REQUEST = dict(mem_limit=1000, cpu_period=100, cpu_quota=50)

    def setUp(self):
        super().setUp()
        self.sc.hibernate_local_instance("a")

    def _wake(self, revive, **patches):
        with mock.patch.object(hibernation, "_revive_locked", side_effect=revive) as revive_mock, \
                mock.patch("src.manager.modify_resources.could_ve_this_extra_memory",
                           return_value=patches.get("fits", True)):
            woke = hibernation.wake_instance("a", _sysreq(**self.REQUEST))
        return woke, revive_mock

    def test_it_starts_the_guest_on_its_own_disk_with_the_resources_asked(self):
        woke, revive = self._wake(lambda token: self.sc.resume_local_instance(token))
        self.assertTrue(woke)
        revive.assert_called_once_with("a")
        self.assertFalse(self.sc.is_local_instance_suspended("a"))
        row = self.sc.get_sys_req(id="a")
        self.assertEqual((row["mem_limit"], row["cpu_quota"]), (1000, 50))

    def test_a_wake_that_does_not_fit_the_pool_leaves_it_hibernated(self):
        woke, revive = self._wake(lambda token: True, fits=False)
        self.assertFalse(woke)
        revive.assert_not_called()
        self.assertTrue(self.sc.is_local_instance_hibernated("a"))
        self.assertEqual(self.sc.get_sys_req(id="a")["mem_limit"], 0)

    def test_a_guest_that_does_not_start_leaves_it_hibernated_with_its_row_zeroed(self):
        def fail(token):
            raise RuntimeError("no room")
        woke, _ = self._wake(fail)
        self.assertFalse(woke)
        self.assertTrue(self.sc.is_local_instance_hibernated("a"))
        row = self.sc.get_sys_req(id="a")
        self.assertEqual((row["mem_limit"], row["cpu_period"], row["cpu_quota"]), (0, 0, 0))

    def test_a_running_instance_is_not_woken(self):
        self.sc.resume_local_instance("a")
        self.assertFalse(hibernation.wake_instance("a", _sysreq(**self.REQUEST)))


class TickTests(HibernationTestCase):
    def setUp(self):
        super().setUp()
        self.sc.hibernate_local_instance("a")
        maintain._local_charged_at.clear()
        self.addCleanup(maintain._local_charged_at.clear)

    def test_a_top_up_does_not_revive_a_hibernated_instance(self):
        rows = self.sc.get_suspended_local_instances()
        self.assertEqual(rows[0]["suspended_reason"], dead_instances.HIBERNATED)
        with mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "_can_pay_a_tick", return_value=True), \
                mock.patch.object(maintain, "system_scarcity", return_value={}), \
                mock.patch.object(maintain, "revive_instance") as revive:
            self.assertEqual(maintain.revive_suspended_instances(now=1000.0), 0)
        revive.assert_not_called()

    def test_the_grace_period_does_not_delete_a_hibernated_instance(self):
        # No balance and a grace long gone: anything but a hibernated instance would go.
        rows = [dict(self.sc.get_suspended_local_instances()[0],
                     suspended_for_s=10 ** 9, balance_mu=0)]
        with mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "stop_instance") as stop:
            self.assertEqual(maintain.reap_expired_suspensions(), 0)
        stop.assert_not_called()

    def test_it_is_charged_for_its_disk_and_nothing_else(self):
        rows = self.sc.get_hibernated_local_instances()
        seen = {}

        def price(system_resources, **kwargs):
            seen["resources"] = system_resources
            return 7

        with mock.patch.object(maintain, "compute_maintenance_cost", side_effect=price), \
                mock.patch.object(maintain, "spend_mu", return_value=True) as spend:
            charged = maintain._charge_hibernated(rows, scarcity={})
        self.assertEqual(charged, 7)
        resources = seen["resources"]
        self.assertEqual((resources.mem_limit, resources.cpu_quota, resources.disk_space), (0, 0, 500))
        self.assertEqual(spend.call_args.kwargs["amount_mu"], 7)

    def _charge(self, pays: bool, grace: int = 7 * 24 * 3600):
        rows = self.sc.get_hibernated_local_instances()
        with mock.patch.object(maintain, "compute_maintenance_cost", return_value=7), \
                mock.patch.object(maintain, "spend_mu", return_value=pays), \
                mock.patch.object(maintain.dead_instances, "suspended_grace_seconds", return_value=grace), \
                mock.patch.object(maintain, "stop_instance") as stop:
            maintain._charge_hibernated(rows, scarcity={})
        return stop

    def test_one_that_cannot_pay_keeps_its_disk_and_its_grace_period_starts(self):
        stop = self._charge(pays=False)
        stop.assert_not_called()
        self.assertTrue(self.sc.is_local_instance_hibernated("a"))
        (row,) = self.sc.get_hibernated_local_instances()
        self.assertEqual(row["suspended_reason"], dead_instances.HIBERNATED_OUT_OF_BALANCE)

    def test_with_no_grace_it_is_stopped_with_its_disk(self):
        stop = self._charge(pays=False, grace=0)
        stop.assert_called_once_with(token="a", reason=dead_instances.OUT_OF_BALANCE)

    def test_a_top_up_that_lets_it_pay_ends_the_grace(self):
        self._charge(pays=False)
        self._charge(pays=True)
        (row,) = self.sc.get_hibernated_local_instances()
        self.assertEqual(row["suspended_reason"], dead_instances.HIBERNATED)

    def test_the_grace_clock_starts_when_it_cannot_pay_not_when_it_hibernated(self):
        self.sc._execute("UPDATE local_instances SET suspended_at = '2000-01-01 00:00:00' WHERE id = 'a'")
        self._charge(pays=False)
        (row,) = self.sc.get_suspended_local_instances()
        self.assertLess(row["suspended_for_s"], 60)

    def test_when_the_grace_ends_unpaid_it_is_deleted_with_its_disk(self):
        self._charge(pays=False)
        rows = [dict(self.sc.get_suspended_local_instances()[0], suspended_for_s=10 ** 9, balance_mu=0)]
        with mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "stop_instance", return_value=0) as stop:
            self.assertEqual(maintain.reap_expired_suspensions(), 1)
        stop.assert_called_once_with(token="a", reason=dead_instances.OUT_OF_BALANCE)

    def test_a_top_up_does_not_revive_one_that_was_out_of_balance_either(self):
        self._charge(pays=False)
        rows = self.sc.get_suspended_local_instances()
        with mock.patch.object(maintain.sc, "get_suspended_local_instances", return_value=rows), \
                mock.patch.object(maintain, "_can_pay_a_tick", return_value=True), \
                mock.patch.object(maintain, "system_scarcity", return_value={}), \
                mock.patch.object(maintain, "revive_instance") as revive:
            self.assertEqual(maintain.revive_suspended_instances(now=1000.0), 0)
        revive.assert_not_called()

    def test_one_that_was_out_of_balance_is_still_woken_by_a_resize(self):
        self._charge(pays=False)
        self.assertEqual(
            hibernation.plan("a", _sysreq(mem_limit=1000, cpu_period=100, cpu_quota=50)),
            hibernation.WAKE)


class GatewayTests(HibernationTestCase):
    """`ModifyServiceSystemResources` with a token, and what it does with each plan."""

    def _call(self, request, *, client_id="", peer_ip="10.0.0.9", spend=True):
        payload = (request, client_id)
        context = mock.Mock(peer=lambda: f"ipv4:{peer_ip}:5555")
        with mock.patch.object(gateway, "parse_with_client", return_value=payload), \
                mock.patch.object(gateway, "spend_mu", return_value=spend) as charge, \
                mock.patch.object(gateway, "get_internal_service_id_by_uri", return_value=None):
            replies = list(gateway.Gateway.ModifyServiceSystemResources(mock.Mock(), iter(()), context))
        return replies, charge

    def test_a_token_names_the_instance_whatever_the_address(self):
        request = celaut_pb2.ModifyServiceSystemResourcesInput(service_token="a")
        with mock.patch.object(gateway, "hotplug", return_value=True) as hotplug:
            _, charge = self._call(request)
        hotplug.assert_called_once()
        self.assertEqual(hotplug.call_args.kwargs["vmachine_id"], "a")
        self.assertEqual(charge.call_args.kwargs["id"], "a")

    def test_a_token_of_a_child_of_a_client_needs_that_client(self):
        request = celaut_pb2.ModifyServiceSystemResourcesInput(service_token="a")
        with mock.patch.object(gateway, "require_father", side_effect=gateway.ClientRequired("no")):
            with self.assertRaises(gateway.ClientRequired):
                self._call(request)

    def test_an_unknown_token_is_refused_and_nothing_is_charged(self):
        request = celaut_pb2.ModifyServiceSystemResourcesInput(service_token="nobody")
        with self.assertRaises(Exception) as raised:
            _, charge = self._call(request)
        self.assertIn("no local instance", str(raised.exception))

    def test_hibernating_answers_before_the_vm_is_stopped(self):
        request = celaut_pb2.ModifyServiceSystemResourcesInput(
            service_token="a", max_sysreq=_sysreq(cpu_quota=0))
        order = []
        with mock.patch.object(hibernation, "check_can_hibernate"), \
                mock.patch.object(hibernation, "hibernate_instance",
                                  side_effect=lambda token: order.append("stopped")):
            context = mock.Mock(peer=lambda: "ipv4:10.0.0.9:5555")
            with mock.patch.object(gateway, "parse_with_client", return_value=(request, "")), \
                    mock.patch.object(gateway, "spend_mu", return_value=True):
                for reply in gateway.Gateway.ModifyServiceSystemResources(mock.Mock(), iter(()), context):
                    order.append("answered")
        self.assertEqual(order, ["answered", "stopped"])

    def test_a_refused_hibernation_refunds_the_call_and_stops_nothing(self):
        request = celaut_pb2.ModifyServiceSystemResourcesInput(
            service_token="a", max_sysreq=_sysreq(mem_limit=0))
        refunded = []

        def charge(**kwargs):
            kwargs["refund_function_container"].append(lambda: refunded.append(True))
            return True

        context = mock.Mock(peer=lambda: "ipv4:10.0.0.9:5555")
        with mock.patch.object(hibernation, "check_can_hibernate",
                               side_effect=hibernation.HibernationRefused("shared filesystem")), \
                mock.patch.object(hibernation, "hibernate_instance") as hibernate, \
                mock.patch.object(gateway, "parse_with_client", return_value=(request, "")), \
                mock.patch.object(gateway, "spend_mu", side_effect=charge):
            with self.assertRaises(Exception):
                list(gateway.Gateway.ModifyServiceSystemResources(mock.Mock(), iter(()), context))
        self.assertEqual(refunded, [True])
        hibernate.assert_not_called()

    def test_a_wake_that_fails_refunds_the_call(self):
        self.sc.hibernate_local_instance("a")
        request = celaut_pb2.ModifyServiceSystemResourcesInput(
            service_token="a", max_sysreq=_sysreq(mem_limit=1000, cpu_period=100, cpu_quota=50))
        refunded = []

        def charge(**kwargs):
            kwargs["refund_function_container"].append(lambda: refunded.append(True))
            return True

        context = mock.Mock(peer=lambda: "ipv4:10.0.0.9:5555")
        with mock.patch.object(hibernation, "wake_instance", return_value=False), \
                mock.patch.object(gateway, "parse_with_client", return_value=(request, "")), \
                mock.patch.object(gateway, "spend_mu", side_effect=charge):
            with self.assertRaises(Exception):
                list(gateway.Gateway.ModifyServiceSystemResources(mock.Mock(), iter(()), context))
        self.assertEqual(refunded, [True])


if __name__ == "__main__":
    unittest.main()
