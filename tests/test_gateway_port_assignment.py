"""Clearing the gateway port BEFORE it is written to config.yaml.

The incident: a host picked a port, wrote it down, opened it in nodo's own nftables
table, and started serving. Another ruleset on the same input hook rejected it, so
peers on the same LAN got "no route to host" while the node looked healthy from the
inside. nodo's accept rule was correct and irrelevant -- in nftables 'accept' ends
the evaluation of its own chain only, and a reject elsewhere on the hook wins
regardless of priority.

Assignment used to skip verification entirely, for a real reason: nothing is
listening at that moment, and a connect to a port with no listener fails whether or
not the firewall allows it. So the probe now brings its own throwaway listener, and
a port that still cannot be cleared is not stored -- an unassigned port stops the
node with an explanation, which is strictly better than a node that answers nothing.

What nodo does NOT do is drive the firewall for the operator. It writes nftables
(or iptables) rules and reads the ruleset back; which program owns the rest is not
something it assumes, so the failure states the property the host must satisfy
rather than commands for one distro's front-end.
"""
import socket
import subprocess
import unittest
from unittest.mock import patch

from src.utils.firewall.backends import ForeignRejector, NftBackend
from src.utils.firewall import gateway
from src.utils.firewall.gateway import GatewayPortUnavailable, assign_gateway_port
from src.utils.firewall.reachability import ProbeResult, probe_tcp_from_bridge
from tests.test_gateway_port_reachability import (
    BRIDGE,
    GATEWAY_IP,
    PORT,
    SUBNET,
    FakeRunner,
    _proc,
)

FIREWALLD_REJECTOR = ForeignRejector(
    table="inet firewalld",
    chain="filter_INPUT",
    priority=10,
    reason="contains a reject rule",
)


class HookContractTests(unittest.TestCase):
    """What the operator is told: a property to establish, not a tool to run.

    nodo speaks the two interfaces the kernel offers. Naming a front-end would be
    wrong on every host that uses a different one, and telling an operator to run a
    command that does not exist there costs more trust than saying nothing.
    """

    def _text(self):
        return "\n".join(gateway._hook_contract(PORT, BRIDGE, SUBNET))

    def test_it_states_the_property_in_ruleset_terms(self):
        text = self._text()
        self.assertIn(f"TCP {PORT} inbound must be accepted", text)
        self.assertIn("input", text)          # the hook it has to hold on
        self.assertIn(SUBNET, text)           # guests
        self.assertIn(BRIDGE, text)
        self.assertIn("peers", text)          # and the outside

    def test_it_names_no_firewall_front_end(self):
        text = self._text().lower()
        for tool in ("firewall-cmd", "firewalld", "ufw", "yast", "shorewall"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, text)

    def test_it_suggests_no_commands_at_all(self):
        # Not even a "sudo": the host's ruleset may be managed from a template or a
        # config-management run, where an ad-hoc command would be reverted anyway.
        self.assertNotIn("sudo", self._text())

    def test_it_says_which_interfaces_nodo_itself_speaks(self):
        # So an operator knows where nodo's own rules live, and that nothing else
        # on the host is being written to behind their back.
        text = self._text()
        self.assertIn("nftables", text)
        self.assertIn("iptables", text)


@patch("src.utils.firewall.gateway.os.geteuid", return_value=0)
class AssignGatewayPortTests(unittest.TestCase):
    """The assignment decision itself: store the port, or refuse and explain."""

    def setUp(self):
        self.rejectors = []
        self.logged = []

        backend_patch = patch(
            "src.utils.firewall.gateway.detect_backend", side_effect=self._backend
        )
        backend_patch.start()
        self.addCleanup(backend_patch.stop)

    def _backend(self, *_args, **_kwargs):
        backend = NftBackend(run=FakeRunner())
        backend.list_rules = lambda chain, comment_prefix="": []
        backend.add = lambda rule: None
        backend.delete = lambda applied: None
        backend.foreign_input_rejectors = lambda: self.rejectors
        return backend

    def _assign(self):
        return assign_gateway_port(
            port=PORT,
            bridge=BRIDGE,
            gateway_ip=GATEWAY_IP,
            subnet=SUBNET,
            config_path="/nodo/config.yaml",
            log=self.logged.append,
        )

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_a_port_proven_reachable_is_cleared(self, mock_probe, _euid):
        mock_probe.return_value = ProbeResult(True, "connected")
        self.assertIs(self._assign().reachable, True)

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_the_probe_is_asked_to_bring_its_own_listener(self, mock_probe, _euid):
        # Without this the probe can only answer "unknown" at assignment time,
        # which is how an unverified port used to get stored.
        mock_probe.return_value = ProbeResult(True, "connected")
        self._assign()
        self.assertIs(mock_probe.call_args.kwargs["provide_listener"], True)

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_a_provably_blocked_port_is_refused(self, mock_probe, _euid):
        mock_probe.return_value = ProbeResult(False, "connect refused")
        self.rejectors = [FIREWALLD_REJECTOR]

        with self.assertRaises(GatewayPortUnavailable) as ctx:
            self._assign()
        self.assertEqual(ctx.exception.port, PORT)

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_an_unverifiable_port_is_refused_when_something_can_reject_it(
        self, mock_probe, _euid
    ):
        # The Fedora case: the bridge does not exist yet so nothing can be proven,
        # and firewalld is sitting on the input hook. Storing the port here is what
        # produced a node that looked alive and answered nothing.
        mock_probe.return_value = ProbeResult(None, "bridge br-ch does not exist yet")
        self.rejectors = [FIREWALLD_REJECTOR]

        with self.assertRaises(GatewayPortUnavailable) as ctx:
            self._assign()

        instructions = ctx.exception.instructions
        self.assertIn("filter_INPUT", instructions)          # names what can reject
        self.assertIn("must be accepted", instructions)      # states what has to hold
        self.assertIn("GATEWAY_PORT", instructions)          # and the manual way out
        self.assertIn("nat-guide", instructions)             # outside the LAN is separate

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_an_unverifiable_port_is_accepted_when_nothing_can_reject_it(
        self, mock_probe, _euid
    ):
        # A fresh node whose guest bridge has never been created is the ordinary
        # case; refusing here would make a first install unrecoverable.
        mock_probe.return_value = ProbeResult(None, "bridge br-ch does not exist yet")
        self.rejectors = []

        result = self._assign()

        self.assertIsNone(result.reachable)
        self.assertTrue(
            any("nothing outside nodo's ruleset" in line for line in self.logged),
            self.logged,
        )

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_the_refusal_explains_the_problem_instead_of_prescribing_a_tool(
        self, mock_probe, _euid
    ):
        # The operator knows what owns their firewall; nodo does not. It reports the
        # rejecting chain it read from the ruleset and the property that must hold.
        mock_probe.return_value = ProbeResult(None, "bridge br-ch does not exist yet")
        self.rejectors = [FIREWALLD_REJECTOR]

        with self.assertRaises(GatewayPortUnavailable) as ctx:
            self._assign()

        instructions = ctx.exception.instructions
        self.assertIn("inet firewalld / filter_INPUT", instructions)  # read, not assumed
        self.assertIn(f"TCP {PORT} inbound must be accepted", instructions)
        self.assertNotIn("firewall-cmd", instructions)
        self.assertNotIn("sudo", instructions)


@patch("src.utils.firewall.reachability.os.geteuid", return_value=0)
class ProbeWithSuppliedListenerTests(unittest.TestCase):
    """``provide_listener`` is what makes a port checkable before the node is up."""

    @staticmethod
    def _free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_without_a_listener_and_without_the_flag_nothing_is_attempted(self, _euid):
        runner = FakeRunner()
        result = probe_tcp_from_bridge(
            bridge=BRIDGE,
            target_ip=GATEWAY_IP,
            port=self._free_port(),
            subnet=SUBNET,
            run=runner,
            sleep=lambda _s: None,
        )
        self.assertIsNone(result.reachable)
        self.assertIn("nothing is listening", result.detail)
        self.assertFalse(runner.ran("ip", "netns", "add"))

    def test_with_the_flag_the_probe_actually_runs(self, _euid):
        port = self._free_port()
        runner = FakeRunner({("ip", "netns", "exec"): _proc(0, "connected")})

        result = probe_tcp_from_bridge(
            bridge=BRIDGE,
            target_ip=GATEWAY_IP,
            port=port,
            subnet=SUBNET,
            run=runner,
            sleep=lambda _s: None,
            provide_listener=True,
        )

        self.assertIs(result.reachable, True)
        self.assertTrue(runner.ran("ip", "netns", "add"))

    def test_the_supplied_listener_is_released_afterwards(self, _euid):
        # A socket left bound would collide with the gateway that is about to bind
        # this very port.
        port = self._free_port()
        probe_tcp_from_bridge(
            bridge=BRIDGE,
            target_ip=GATEWAY_IP,
            port=port,
            subnet=SUBNET,
            run=FakeRunner({("ip", "netns", "exec"): _proc(0, "connected")}),
            sleep=lambda _s: None,
            provide_listener=True,
        )

        with socket.socket() as after:
            after.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            after.bind(("", port))  # raises if the probe kept it

    def test_a_port_held_by_something_else_is_unknown_not_closed(self, _euid):
        held = socket.socket()
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("", 0))
        held.listen(1)
        self.addCleanup(held.close)
        port = held.getsockname()[1]

        # Something IS listening, so the probe runs against it rather than
        # inventing a listener of its own.
        runner = FakeRunner({("ip", "netns", "exec"): _proc(0, "connected")})
        result = probe_tcp_from_bridge(
            bridge=BRIDGE,
            target_ip=GATEWAY_IP,
            port=port,
            subnet=SUBNET,
            run=runner,
            sleep=lambda _s: None,
            provide_listener=True,
        )
        self.assertIs(result.reachable, True)


if __name__ == "__main__":
    unittest.main()
