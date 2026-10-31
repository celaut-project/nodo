"""Guest address allocation is atomic with its reservation (issue #607).

A launch picks its address seconds before the runtime state or database row records
it. Two concurrent launches must still never be handed the same one, a failed launch
must give its address back, and the reachability probe must stay off reserved ones.
"""
import ipaddress
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from src.utils.firewall import reachability
from src.virtualizers.microvm import network


def _slow_empty_states():
    time.sleep(0.02)  # the state writer: a read that gives the other threads time to race
    return {}


class _Patched(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(network, "list_runtime_states", side_effect=_slow_empty_states),
            patch.object(network, "_probe_held_ips", return_value=set()),
            patch.object(network.sc, "get_all_internal_containers_ids", return_value=[]),
            patch.object(network.sc, "get_internal_ip", return_value=None),
            patch.object(network, "NETWORK_SUBNET", "192.168.200.0/29"),  # 6 hosts, .1 is the gateway
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: getattr(network, "_reservations", {}).clear())


class ConcurrentAllocationTests(_Patched):
    def test_concurrent_launches_never_share_an_address(self):
        ids = [f"vm-{i}" for i in range(5)]  # exactly the five free addresses
        barrier = threading.Barrier(len(ids))
        got, errors = {}, []

        def launch(vmachine_id):
            barrier.wait()
            try:
                got[vmachine_id] = network.deterministic_ip_and_mac(vmachine_id)[0]
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=launch, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(set(got.values())), len(ids), got)
        self.assertNotIn(network.NETWORK_GATEWAY_IP, got.values())


class ReservationLifecycleTests(_Patched):
    def test_a_reservation_counts_against_everyone_else_until_released(self):
        ip, _mac = network.deterministic_ip_and_mac("vm-a")
        self.assertIn(ip, network.used_ips())
        network.release_ip("vm-a")
        self.assertNotIn(ip, network.used_ips())

    def test_the_same_id_gets_the_same_address_while_reserved(self):
        first = network.deterministic_ip_and_mac("vm-a")
        network.deterministic_ip_and_mac("vm-b")
        self.assertEqual(network.deterministic_ip_and_mac("vm-a"), first)

    def test_releasing_an_id_that_holds_nothing_is_harmless(self):
        network.release_ip("nobody")

    def test_a_released_address_can_be_handed_out_again(self):
        ip, _mac = network.deterministic_ip_and_mac("vm-a")
        network.release_ip("vm-a")
        self.assertEqual(network.deterministic_ip_and_mac("vm-a")[0], ip)


class FailedLaunchReleasesTests(_Patched):
    def test_a_ch_launch_that_fails_gives_its_address_back(self):
        from protos import celaut_pb2 as celaut
        from src.virtualizers.ch import execute as ch_execute
        from src.virtualizers.microvm.errors import MicroVMError

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime_dir = Path(tmp.name) / "vm"
        runtime_dir.mkdir()
        bundle = {"arch": "x86_64", "rootfs_path": "r", "kernel_path": "k",
                  "initramfs_path": "i", "rootfs_format": "ext4"}
        seen = {}
        real = network.deterministic_ip_and_mac

        def allocate(vmachine_id):
            seen["ip"] = real(vmachine_id)[0]
            return real(vmachine_id)

        for p in (
            mock.patch.object(ch_execute.paths, "runtime_vm_dir", return_value=runtime_dir),
            mock.patch.object(ch_execute.network, "preflight"),
            mock.patch.object(ch_execute, "_resolve_ch_binary", return_value="ch"),
            mock.patch.object(ch_execute.microvm_bundle, "resolve_service_arch", return_value="x86_64"),
            mock.patch.object(ch_execute.microvm_bundle, "load_bundle", return_value=bundle),
            mock.patch.object(ch_execute.microvm_bundle, "validate_custom_initramfs"),
            mock.patch.object(ch_execute.microvm_bundle, "validate_entrypoint_strict", return_value="/run"),
            mock.patch.object(ch_execute.network, "deterministic_ip_and_mac", side_effect=allocate),
            mock.patch.object(ch_execute, "ensure_private_dir"),
            mock.patch.object(ch_execute.network, "replay_legacy_cleanup_rules"),
            mock.patch.object(ch_execute, "vm_remove_vm_rules"),
            mock.patch.object(ch_execute, "release_shares"),
            mock.patch.object(ch_execute, "delete_runtime_state"),
            mock.patch.object(ch_execute, "log"),
        ):
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(MicroVMError):
            # No launch spec: the revive fails after the address was picked.
            ch_execute.execute(
                assigment_ports=None, by_local=True, service_id="svc",
                service=celaut.Service(), config=None,
                system_resources=celaut.Service.Container.Resources(),
                father_id="father", revive_id="vm")
        self.assertIn("ip", seen)
        self.assertNotIn(seen["ip"], network.used_ips())


class ProbeAvoidsReservedAddressesTests(unittest.TestCase):
    def test_the_probe_skips_addresses_the_node_has_reserved(self):
        subnet, gateway = "192.168.200.0/29", "192.168.200.1"
        top = "192.168.200.6"  # the probe's first choice
        runner = lambda args: SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(network, "used_ips", return_value={top}):
            in_use = reachability._addresses_in_use(runner, "br")
        self.assertIn(top, in_use)
        (picked,), _prefix = reachability._pick_source_ips(subnet, gateway, in_use)
        self.assertNotEqual(picked, top)
        self.assertIn(ipaddress.ip_address(picked), ipaddress.ip_network(subnet))

    def test_an_unreadable_node_state_does_not_fail_the_probe(self):
        runner = lambda args: SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(network, "used_ips", side_effect=RuntimeError("db down")):
            self.assertEqual(reachability._addresses_in_use(runner, "br"), set())


if __name__ == "__main__":
    unittest.main()
