"""Proving the gateway port is reachable, instead of trusting the rule.

The incident behind this file: a correct accept rule for the gateway port sat in
the ruleset while every guest's gRPC call was rejected by a higher-priority chain
in another table. The rule was there, the port was shut, and nothing noticed --
because nothing ever tried. Checking from the host proves nothing either: that
packet goes out over ``lo``, which almost everything accepts. So the probe sends
the packet the way a guest does, from a throwaway namespace on the guest bridge.
"""
import subprocess
import unittest
from unittest.mock import patch

from src.utils.firewall.backends import ForeignRejector, InputRule, NftBackend
from src.utils.firewall.rules import Chain
from src.utils.firewall.gateway import (
    GatewayPortUnavailable,
    ensure_gateway_port_open,
    gateway_comment,
)
from src.utils.firewall.reachability import ProbeResult, probe_tcp_from_bridge

BRIDGE = "br-ch"
GATEWAY_IP = "192.168.200.1"
SUBNET = "192.168.200.0/24"
PORT = 58443


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

    def ran(self, *prefix):
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


def _probe(runner, **kwargs):
    return probe_tcp_from_bridge(
        bridge=BRIDGE,
        target_ip=GATEWAY_IP,
        port=PORT,
        subnet=SUBNET,
        run=runner,
        sleep=lambda _seconds: None,
        **kwargs,
    )


@patch("src.utils.firewall.reachability.os.geteuid", return_value=0)
class ProbeTests(unittest.TestCase):
    def setUp(self):
        # Every probe here assumes the gateway is already listening; with nothing
        # bound the probe never runs at all (NoListenerTests below).
        patcher = patch(
            "src.utils.firewall.reachability._listener_present", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_successful_connect_is_conclusive(self, _euid):
        runner = FakeRunner()
        result = _probe(runner)

        self.assertIs(result.reachable, True)
        self.assertTrue(runner.ran("ip", "netns", "exec"))
        self.assertIn(f"{GATEWAY_IP}:{PORT}", result.detail)

    def test_a_refused_connect_is_conclusive_the_other_way(self, _euid):
        runner = FakeRunner(
            {("ip", "netns", "exec"): _proc(1, "ConnectionRefusedError: [Errno 111]")}
        )
        result = _probe(runner, attempts=2)

        self.assertIs(result.reachable, False)
        self.assertIn("ConnectionRefusedError", result.detail)
        self.assertIn("2 attempt(s)", result.detail)

    def test_the_namespace_and_veth_are_always_torn_down(self, _euid):
        runner = FakeRunner({("ip", "netns", "exec"): _proc(1, "timed out")})
        _probe(runner, attempts=1)

        self.assertTrue(runner.ran("ip", "netns", "del"))
        self.assertTrue(runner.ran("ip", "link", "del"))

    def test_a_missing_bridge_is_unknown_not_closed(self, _euid):
        runner = FakeRunner({("ip", "link", "show"): _proc(1, "", "does not exist")})
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertFalse(result.conclusive)
        self.assertIn(BRIDGE, result.detail)
        # Nothing was built, so there is nothing to tear down.
        self.assertFalse(runner.ran("ip", "netns", "add"))

    def test_a_source_address_already_in_use_is_avoided(self, _euid):
        # .254 is taken by a neighbour, so the probe must pick something else.
        runner = FakeRunner(
            {
                ("ip", "neigh", "show"): _proc(
                    0, "192.168.200.254 dev br-ch lladdr 02:aa:bb:cc:dd:ee REACHABLE\n"
                ),
            }
        )
        result = _probe(runner)

        self.assertIs(result.reachable, True)
        self.assertNotEqual(result.source_ip, "192.168.200.254")
        self.assertNotEqual(result.source_ip, GATEWAY_IP)

    def test_a_failure_to_build_the_namespace_is_unknown(self, _euid):
        runner = FakeRunner({("ip", "netns", "add"): _proc(1, "", "Operation not permitted")})
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertIn("could not create netns", result.detail)


@patch("src.utils.firewall.reachability.os.geteuid", return_value=0)
@patch("src.utils.firewall.reachability._listener_present", return_value=False)
class NoListenerTests(unittest.TestCase):
    """A port nothing is bound to answers like a blocked one. That is not a verdict.

    This is what made a fresh install unrecoverable: the port is opened *before*
    anything listens on it, so the probe's connect always failed, the assignment
    was rolled back, and ``GATEWAY_PORT`` stayed ``auto`` forever on any host whose
    guest bridge already existed.
    """

    def test_no_listener_is_unknown_not_closed(self, _listener, _euid):
        runner = FakeRunner({("ip", "netns", "exec"): _proc(1, "ConnectionRefusedError")})
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertIn("nothing is listening", result.detail)
        # And no namespace was built to find that out.
        self.assertFalse(runner.ran("ip", "netns", "add"))


class ProbePrivilegeTests(unittest.TestCase):
    @patch("src.utils.firewall.reachability.os.geteuid", return_value=1000)
    def test_without_root_the_answer_is_unknown(self, _euid):
        runner = FakeRunner()
        result = _probe(runner)

        self.assertIsNone(result.reachable)
        self.assertIn("root", result.detail)
        self.assertEqual(runner.calls, [], "nothing should be attempted without root")


@patch("src.utils.firewall.gateway.os.geteuid", return_value=0)
class EnsureGatewayPortOpenTests(unittest.TestCase):
    def _backend(self, existing=()):
        """A backend whose primitives are recorded rather than executed."""
        backend = NftBackend(run=FakeRunner())
        backend.list_rules = lambda chain, comment_prefix="": [
            rule for rule in existing if rule.comment.startswith(comment_prefix)
        ]
        backend.add = lambda rule: self.added.append(rule)
        backend.delete = lambda applied: self.removed.append(applied)
        backend.foreign_input_rejectors = lambda: self.rejectors
        return backend

    def setUp(self):
        self.added = []
        self.removed = []
        self.rejectors = []

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_a_provably_closed_port_stops_the_node(self, mock_probe, _euid):
        mock_probe.return_value = ProbeResult(False, "connect refused", source_ip="192.168.200.254")
        self.rejectors = [
            ForeignRejector(
                table="inet firewalld",
                chain="filter_INPUT",
                priority=10,
                reason="contains a reject rule",
            )
        ]

        with self.assertRaises(GatewayPortUnavailable) as ctx:
            ensure_gateway_port_open(
                port=PORT,
                bridge=BRIDGE,
                gateway_ip=GATEWAY_IP,
                subnet=SUBNET,
                backend=self._backend(),
            )

        instructions = ctx.exception.instructions
        # It must say why an accept rule did not help, and name what is rejecting.
        self.assertIn("filter_INPUT", instructions)
        self.assertIn("accept", instructions)
        self.assertIn("firewall-cmd", instructions)
        self.assertEqual(ctx.exception.port, PORT)

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_an_unknown_result_only_warns(self, mock_probe, _euid):
        mock_probe.return_value = ProbeResult(None, "bridge br-ch does not exist yet")
        logged = []

        result = ensure_gateway_port_open(
            port=PORT,
            bridge=BRIDGE,
            gateway_ip=GATEWAY_IP,
            subnet=SUBNET,
            backend=self._backend(),
            log=logged.append,
        )

        self.assertIsNone(result.reachable)
        self.assertTrue(any("Could not verify" in line for line in logged), logged)

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_a_rule_for_an_old_port_is_pruned(self, mock_probe, _euid):
        mock_probe.return_value = ProbeResult(True, "connected")
        stale = InputRule(
            chain=Chain.INPUT, comment=gateway_comment(59110), port=59110,
            protocol="tcp", handle=4,
        )

        ensure_gateway_port_open(
            port=PORT,
            bridge=BRIDGE,
            gateway_ip=GATEWAY_IP,
            subnet=SUBNET,
            backend=self._backend(existing=[stale]),
        )

        self.assertEqual([rule.comment for rule in self.removed], [stale.comment])
        self.assertEqual([rule.dport for rule in self.added], [PORT])

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_an_existing_rule_is_not_duplicated(self, mock_probe, _euid):
        mock_probe.return_value = ProbeResult(True, "connected")
        current = InputRule(
            chain=Chain.INPUT, comment=gateway_comment(PORT), port=PORT,
            protocol="tcp", handle=9,
        )

        ensure_gateway_port_open(
            port=PORT,
            bridge=BRIDGE,
            gateway_ip=GATEWAY_IP,
            subnet=SUBNET,
            backend=self._backend(existing=[current]),
        )

        self.assertEqual(self.added, [])
        self.assertEqual(self.removed, [])

    def test_verification_can_be_skipped(self, _euid):
        result = ensure_gateway_port_open(
            port=PORT, backend=self._backend(), verify=False
        )
        self.assertIsNone(result.reachable)
        self.assertEqual([rule.dport for rule in self.added], [PORT])


class EnsureGatewayPortPrivilegeTests(unittest.TestCase):
    @patch("src.utils.firewall.gateway.os.geteuid", return_value=1000)
    def test_opening_a_port_without_root_is_refused_with_instructions(self, _euid):
        with self.assertRaises(GatewayPortUnavailable) as ctx:
            ensure_gateway_port_open(port=PORT, verify=False)
        self.assertIn("root", str(ctx.exception))
        self.assertIn("sudo nodo serve", ctx.exception.instructions)


if __name__ == "__main__":
    unittest.main()
