"""Proving a guest can reach another guest, instead of trusting nodo's own accept.

The incident behind this file: a packed service was accused of shortchanging its
child's memory. It had not. The parent's connect to the child timed out because a
foreign forward chain dropped the packet, and a connect timeout looks exactly like
a child that died on boot. nodo's own table had accepted it at priority -5, which
settles nothing -- ``accept`` ends its own chain only.

So the check sends the packet down the real path: two throwaway namespaces on the
guest bridge, wired like real taps (isolated ports included, or the frames would
be switched inside the bridge and never reach the hook that drops them).
"""
import subprocess
import unittest
from unittest.mock import patch

from src.utils.firewall.reachability import GUEST_PROBE_PORT, probe_tcp_between_guests

BRIDGE = "nodo-br-ch"
GATEWAY_IP = "192.168.200.1"
SUBNET = "192.168.200.0/24"


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
        for prefix, result in self.responses.items():
            if command[: len(prefix)] == list(prefix):
                return result
        return self.default

    def commands(self):
        return [" ".join(str(part) for part in call) for call in self.calls]

    def matching(self, *needles):
        return [line for line in self.commands() if all(n in line for n in needles)]


class FakeListener:
    """Stands in for the Popen holding the peer's socket open."""

    def __init__(self, exit_code=None, output="bind failed"):
        self.exit_code = exit_code
        self.output = output
        self.returncode = exit_code
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = -15

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        self.exit_code = -9

    def communicate(self, timeout=None):
        return (self.output, None)


class FakeSpawner:
    def __init__(self, listener=None):
        self.listener = listener or FakeListener()
        self.commands = []

    def __call__(self, command):
        self.commands.append([str(part) for part in command])
        return self.listener


def _probe(runner, spawner=None, **kwargs):
    return probe_tcp_between_guests(
        bridge=BRIDGE,
        subnet=SUBNET,
        gateway_ip=GATEWAY_IP,
        run=runner,
        spawn=spawner or FakeSpawner(),
        sleep=lambda _seconds: None,
        **kwargs,
    )


@patch("src.utils.firewall.reachability.os.geteuid", return_value=0)
class GuestToGuestProbeTests(unittest.TestCase):
    def test_a_successful_connect_is_conclusive(self, _euid):
        runner = FakeRunner()
        result = _probe(runner)

        self.assertIs(result.reachable, True)
        self.assertIn(str(GUEST_PROBE_PORT), result.detail)

    def test_both_ends_are_isolated_bridge_ports_like_a_real_tap(self, _euid):
        # The whole point. Two ordinary bridge ports would switch frames tap to tap
        # and never reach the forward hook, so the probe would pass on exactly the
        # host where every real guest fails.
        runner = FakeRunner()
        _probe(runner)

        isolations = runner.matching("type bridge_slave isolated on")
        self.assertEqual(len(isolations), 2, runner.commands())

    def test_a_host_that_refuses_isolation_answers_unknown_not_reachable(self, _euid):
        runner = FakeRunner({
            ("ip", "link", "set", "dev"): _proc(1, "", "Operation not supported"),
        })
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertIn("probe interface", result.detail)

    def test_the_peers_are_routed_through_the_gateway_in_both_directions(self, _euid):
        # Return traffic matters as much as the connect: without a route back, the
        # peer would ARP the client directly, which port isolation has just made
        # impossible, and the probe would blame the firewall for its own wiring.
        runner = FakeRunner()
        _probe(runner)

        routes = runner.matching("route add", f"via {GATEWAY_IP}")
        self.assertEqual(len(routes), 2, runner.commands())

    def test_two_distinct_addresses_off_the_top_of_the_range(self, _euid):
        runner = FakeRunner()
        _probe(runner)

        added = runner.matching("addr add")
        self.assertEqual(len(added), 2)
        self.assertIn("192.168.200.254/24", added[0])
        self.assertIn("192.168.200.253/24", added[1])

    def test_addresses_already_on_the_bridge_are_skipped(self, _euid):
        runner = FakeRunner({
            ("ip", "neigh", "show"): _proc(0, "192.168.200.254 lladdr aa:bb:cc:dd:ee:ff STALE\n"),
        })
        _probe(runner)

        added = " ".join(runner.matching("addr add"))
        self.assertNotIn("192.168.200.254/", added)

    def test_a_failed_connect_is_conclusive_the_other_way(self, _euid):
        runner = FakeRunner({
            ("ip", "netns", "exec"): _proc(1, "ConnectTimeoutError: timed out"),
        })
        result = _probe(runner)

        self.assertIs(result.reachable, False)
        self.assertIn("timed out", result.detail)

    def test_a_listener_that_never_came_up_is_unknown_not_a_failure(self, _euid):
        # Otherwise the probe's own broken end reads as a broken host.
        runner = FakeRunner({("ip", "netns", "exec"): _proc(1, "ConnectionRefusedError")})
        spawner = FakeSpawner(FakeListener(exit_code=1, output="Address already in use"))
        result = _probe(runner, spawner)

        self.assertIsNone(result.reachable)
        self.assertIn("Address already in use", result.detail)

    def test_a_listener_that_dies_mid_probe_is_unknown_too(self, _euid):
        class DyingListener(FakeListener):
            def __init__(self):
                super().__init__(exit_code=None, output="killed")
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls == 1 else 9

        runner = FakeRunner({("ip", "netns", "exec"): _proc(1, "ConnectTimeoutError")})
        result = _probe(runner, FakeSpawner(DyingListener()))

        self.assertIsNone(result.reachable)
        self.assertIn("exited mid-probe", result.detail)

    def test_a_host_that_does_not_forward_at_all_is_unknown(self, _euid):
        runner = FakeRunner({("sysctl", "-n", "net.ipv4.ip_forward"): _proc(0, "0\n")})
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertIn("ip_forward", result.detail)

    def test_no_bridge_yet_is_unknown(self, _euid):
        runner = FakeRunner({("ip", "link", "show"): _proc(1, "", "does not exist")})
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertIn("does not exist yet", result.detail)

    def test_every_namespace_and_link_is_torn_down(self, _euid):
        runner = FakeRunner()
        _probe(runner)

        self.assertEqual(len(runner.matching("ip netns del")), 2, runner.commands())
        self.assertEqual(len(runner.matching("ip link del")), 2, runner.commands())

    def test_teardown_happens_even_when_the_probe_gives_up_halfway(self, _euid):
        runner = FakeRunner({
            ("ip", "link", "add"): _proc(0),
            ("ip", "link", "set"): _proc(1, "", "no such device"),
        })
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertEqual(len(runner.matching("ip netns del")), 1, runner.commands())

    def test_the_listener_is_stopped_on_the_way_out(self, _euid):
        spawner = FakeSpawner()
        _probe(FakeRunner(), spawner)

        self.assertTrue(spawner.listener.terminated)


class GuestToGuestPrivilegeTests(unittest.TestCase):
    @patch("src.utils.firewall.reachability.os.geteuid", return_value=1000)
    def test_without_root_the_answer_is_unknown(self, _euid):
        result = _probe(FakeRunner())

        self.assertIsNone(result.reachable)
        self.assertIn("root", result.detail)


if __name__ == "__main__":
    unittest.main()
