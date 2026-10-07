"""A leaked probe namespace must not take a guest's address (#493).

The incident behind this file: two child VMs booted, logged that they were
listening, and were never reached. Both had been given 192.168.200.253, which a
reachability-probe namespace left behind by a dead process still held on the
guest bridge. The bridge sent their traffic to the probe's veth. The VM allocator
had no record of the probe; the probe looks at the bridge only when it runs.

So the allocator now skips what probe namespaces hold, and the daemon deletes the
ones nothing owns any more.
"""
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from src.utils.firewall import reachability
from src.utils.firewall.reachability import (
    LEAKED_PROBE_AGE_S,
    probe_held_addresses,
    sweep_leaked_probes,
)


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = default if default is not None else _proc(0)
        self.calls = []

    def __call__(self, command):
        command = list(command)
        self.calls.append(command)
        for match, result in self.responses.items():
            if all(part in command for part in match):
                return result
        return self.default

    def ran(self, *parts):
        return any(all(part in call for part in parts) for call in self.calls)


class NetnsDirTestCase(unittest.TestCase):
    """A fake /run/netns: one file per namespace, its mtime the namespace's age."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(reachability, "NETNS_MOUNT_DIRS", (self.tmp.name,))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The iproute2 path, so the commands do not depend on nsenter being installed.
        which = patch.object(reachability.shutil, "which", return_value=None)
        which.start()
        self.addCleanup(which.stop)
        self.now = time.time()

    def namespace(self, name, age_s=0.0):
        path = os.path.join(self.tmp.name, name)
        open(path, "w").close()
        os.utime(path, (self.now - age_s, self.now - age_s))
        return path


class ProbeHeldAddressesTests(NetnsDirTestCase):
    def test_reads_the_address_inside_each_probe_namespace(self):
        self.namespace("nodofw1a7774")
        self.namespace("nodofwc4788e")
        self.namespace("someone-elses")
        runner = FakeRunner({
            ("nodofw1a7774", "addr"): _proc(stdout=(
                "1: lo    inet 127.0.0.1/8 scope host lo\n"
                "5: nodofwb1a7774    inet 192.168.200.253/24 brd 192.168.200.255 scope global\n"
            )),
            ("nodofwc4788e", "addr"): _proc(stdout=(
                "5: nodofwcg4788e    inet 192.168.200.250/24 scope global\n"
            )),
        })

        held = probe_held_addresses(run=runner)

        self.assertIn("192.168.200.253", held)
        self.assertIn("192.168.200.250", held)
        self.assertNotIn("127.0.0.1", held)
        self.assertFalse(runner.ran("someone-elses"))

    def test_a_namespace_it_cannot_read_contributes_nothing(self):
        self.namespace("nodofw1a7774")
        runner = FakeRunner(default=_proc(1, stderr="mount of /sys failed"))

        self.assertEqual(probe_held_addresses(run=runner), set())

    def test_no_netns_directory_means_no_probes(self):
        with patch.object(reachability, "NETNS_MOUNT_DIRS", ("/nonexistent/netns",)):
            self.assertEqual(probe_held_addresses(run=FakeRunner()), set())


@patch("src.utils.firewall.reachability.os.geteuid", return_value=0)
class SweepTests(NetnsDirTestCase):
    def sweep(self, runner):
        return sweep_leaked_probes(run=runner, now=lambda: self.now)

    def test_deletes_a_namespace_older_than_any_probe_lives(self, _euid):
        self.namespace("nodofw1a7774", age_s=LEAKED_PROBE_AGE_S + 60)
        runner = FakeRunner()

        removed = self.sweep(runner)

        self.assertEqual(removed, ["nodofw1a7774"])
        self.assertTrue(runner.ran("ip", "netns", "del", "nodofw1a7774"))

    def test_leaves_a_probe_that_is_running_now_alone(self, _euid):
        # `nodo doctor` may be probing from another process at this moment.
        self.namespace("nodofw1a7774", age_s=5)
        runner = FakeRunner({
            ("ip", "-o", "link", "show"): _proc(stdout=(
                "7: nodofwa1a7774@if6: <BROADCAST,MULTICAST,UP> mtu 1500 master nodo-br-ch\n"
            )),
        })

        self.assertEqual(self.sweep(runner), [])
        self.assertFalse(runner.ran("netns", "del"))
        self.assertFalse(runner.ran("link", "del"))

    def test_ignores_namespaces_that_are_not_probes(self, _euid):
        self.namespace("cni-1234", age_s=LEAKED_PROBE_AGE_S * 10)
        runner = FakeRunner()

        self.assertEqual(self.sweep(runner), [])
        self.assertFalse(runner.ran("netns", "del"))

    def test_deletes_a_host_veth_whose_namespace_is_gone(self, _euid):
        # The namespace was deleted but the process died before deleting the pair.
        runner = FakeRunner({
            ("ip", "-o", "link", "show"): _proc(stdout=(
                "3: nodo-br-ch: <BROADCAST,MULTICAST,UP> mtu 1500\n"
                "7: nodofwa1a7774@nodofwb1a7774: <BROADCAST> mtu 1500 master nodo-br-ch\n"
                "8: nodofwb1a7774@nodofwa1a7774: <BROADCAST> mtu 1500\n"
                "9: nodofwch4788e@if2: <BROADCAST> mtu 1500 master nodo-br-ch\n"
                "10: tap-vm1: <BROADCAST> mtu 1500 master nodo-br-ch\n"
            )),
        })

        removed = self.sweep(runner)

        self.assertEqual(removed, ["nodofwa1a7774", "nodofwb1a7774", "nodofwch4788e"])
        self.assertFalse(runner.ran("link", "del", "tap-vm1"))
        self.assertFalse(runner.ran("link", "del", "nodo-br-ch"))

    def test_a_namespace_that_will_not_delete_keeps_its_veth(self, _euid):
        self.namespace("nodofw1a7774", age_s=LEAKED_PROBE_AGE_S + 60)
        runner = FakeRunner({
            ("netns", "del"): _proc(1, stderr="Device or resource busy"),
            ("ip", "-o", "link", "show"): _proc(stdout=(
                "7: nodofwa1a7774@if6: <BROADCAST> mtu 1500 master nodo-br-ch\n"
            )),
        })

        self.assertEqual(self.sweep(runner), [])
        self.assertFalse(runner.ran("link", "del"))

    def test_does_nothing_without_root(self, euid):
        euid.return_value = 1000
        self.namespace("nodofw1a7774", age_s=LEAKED_PROBE_AGE_S + 60)
        runner = FakeRunner()

        self.assertEqual(self.sweep(runner), [])
        self.assertEqual(runner.calls, [])


class AllocatorSkipsProbeAddressesTests(unittest.TestCase):
    def setUp(self):
        from src.virtualizers.microvm import network

        self.network = network
        patcher = patch.object(network, "used_ips", return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_guest_is_never_given_an_address_a_probe_holds(self):
        with patch.object(self.network, "_probe_held_ips", return_value=set()):
            preferred, mac = self.network.deterministic_ip_and_mac("vm-493")

        with patch.object(self.network, "_probe_held_ips", return_value={preferred}):
            ip, same_mac = self.network.deterministic_ip_and_mac("vm-493")

        self.assertNotEqual(ip, preferred)
        self.assertEqual(mac, same_mac)

    def test_an_unreadable_probe_does_not_stop_the_allocation(self):
        with patch(
            "src.utils.firewall.reachability.probe_held_addresses",
            side_effect=OSError("no ip binary"),
        ):
            ip, _mac = self.network.deterministic_ip_and_mac("vm-493")

        self.assertTrue(ip.startswith("192.168.200."))


class ServeSweepTests(unittest.TestCase):
    def test_a_sweep_that_fails_does_not_stop_the_start(self):
        from src import serve as serve_module

        logged = []
        with patch.object(serve_module, "sweep_leaked_probes", side_effect=OSError("boom")):
            with patch.object(serve_module.log, "LOGGER", side_effect=logged.append):
                serve_module._sweep_leaked_probes()

        self.assertTrue(any("leaked reachability-probe" in m for m in logged), logged)


if __name__ == "__main__":
    unittest.main()
