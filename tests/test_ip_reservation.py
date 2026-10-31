"""A guest address belongs to its instance for as long as the instance can come back.

An instance that is suspended or waiting to be revived has no VM, but it keeps its
row, so `used_ips()` keeps counting its address against every other id; and the id
that owns it gets it back whatever the hash would now pick (issue #594).
"""
import unittest
from unittest.mock import patch

from src.virtualizers.microvm import network


def _preferred(vmachine_id: str) -> str:
    with patch.object(network, "used_ips", return_value=set()), \
            patch.object(network, "_probe_held_ips", return_value=set()), \
            patch.object(network, "reserved_ip", return_value=None):
        try:
            return network.deterministic_ip_and_mac(vmachine_id)[0]
        finally:
            network.release_ip(vmachine_id)


class _Isolated(unittest.TestCase):
    """Each test allocates for ids of its own: no reservation outlives it."""

    def setUp(self):
        self.addCleanup(network._reservations.clear)


class UsedIpsTests(unittest.TestCase):
    def test_a_row_without_a_vm_still_reserves_its_address(self):
        with patch.object(network, "list_runtime_states", return_value={}), \
                patch.object(network.sc, "get_all_internal_containers_ids", return_value=["suspended"]), \
                patch.object(network.sc, "get_internal_ip", return_value="192.168.200.42"):
            self.assertIn("192.168.200.42", network.used_ips())


class OwnReservationTests(_Isolated):
    def test_an_instance_comes_back_on_the_address_it_held(self):
        # Not the address its id hashes to: the one it was given, then.
        held = "192.168.200.201"
        self.assertNotEqual(_preferred("vm-594"), held)
        with patch.object(network, "used_ips", return_value={held}), \
                patch.object(network, "_probe_held_ips", return_value=set()), \
                patch.object(network, "reserved_ip", return_value=held):
            ip, mac = network.deterministic_ip_and_mac("vm-594")
        self.assertEqual(ip, held)
        self.assertEqual(mac, _mac("vm-594"))

    def test_a_reservation_outside_the_subnet_is_not_honoured(self):
        with patch.object(network, "used_ips", return_value=set()), \
                patch.object(network, "_probe_held_ips", return_value=set()), \
                patch.object(network, "reserved_ip", return_value="10.9.9.9"):
            ip, _mac_value = network.deterministic_ip_and_mac("vm-594")
        self.assertTrue(ip.startswith("192.168.200."))

    def test_the_gateway_is_never_handed_back_as_a_reservation(self):
        with patch.object(network, "used_ips", return_value=set()), \
                patch.object(network, "_probe_held_ips", return_value=set()), \
                patch.object(network, "reserved_ip", return_value=network.NETWORK_GATEWAY_IP):
            ip, _mac_value = network.deterministic_ip_and_mac("vm-594")
        self.assertNotEqual(ip, network.NETWORK_GATEWAY_IP)

    def test_the_same_id_gets_the_same_mac_and_tap_every_time(self):
        self.assertEqual(_mac("vm-594"), _mac("vm-594"))
        self.assertNotEqual(_mac("vm-594"), _mac("vm-595"))


class NoOneElseGetsItTests(_Isolated):
    def test_a_new_instance_never_gets_a_reserved_address_even_on_a_hash_collision(self):
        # The address that "newcomer" hashes to is the one the suspended instance
        # holds, as if their ids had collided.
        contested = _preferred("newcomer")
        with patch.object(network, "list_runtime_states", return_value={}), \
                patch.object(network, "_probe_held_ips", return_value=set()), \
                patch.object(network.sc, "get_all_internal_containers_ids", return_value=["suspended"]), \
                patch.object(network.sc, "get_internal_ip",
                             side_effect=lambda id: contested if id == "suspended" else None):
            ip, _mac_value = network.deterministic_ip_and_mac("newcomer")
        self.assertNotEqual(ip, contested)

    def test_the_owner_still_gets_it_while_everyone_else_is_refused(self):
        held = _preferred("owner")
        with patch.object(network, "list_runtime_states", return_value={}), \
                patch.object(network, "_probe_held_ips", return_value=set()), \
                patch.object(network.sc, "get_all_internal_containers_ids", return_value=["owner"]), \
                patch.object(network.sc, "get_internal_ip",
                             side_effect=lambda id: held if id == "owner" else None):
            self.assertEqual(network.deterministic_ip_and_mac("owner")[0], held)
            self.assertNotEqual(network.deterministic_ip_and_mac("other")[0], held)


def _mac(vmachine_id: str) -> str:
    with patch.object(network, "used_ips", return_value=set()), \
            patch.object(network, "_probe_held_ips", return_value=set()), \
            patch.object(network, "reserved_ip", return_value=None):
        try:
            return network.deterministic_ip_and_mac(vmachine_id)[1]
        finally:
            network.release_ip(vmachine_id)


if __name__ == "__main__":
    unittest.main()
