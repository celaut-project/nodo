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

nodo still does not drive the firewall for the operator: it writes nftables (or
iptables) rules and reads the ruleset back. But the failure message names the
front-end that is *running on this host* -- detected, not assumed -- and gives the
single command that opens the port with it, falling back to a one-paragraph
statement of what has to hold where there is no front-end to name.
"""
import socket
import subprocess
import unittest
from unittest.mock import patch

from src.utils.firewall.backends import ForeignRejector, NftBackend
from src.utils.firewall.frontend import Frontend
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


class OperatorAdviceTests(unittest.TestCase):
    """What the operator is told: one command when there is one, else the property.

    The old wording refused to name any front-end, which was correct and useless:
    an operator was left to work out for themselves whether firewalld, ufw or
    nothing at all owns their host before they could act. nodo already reads enough
    of the host to answer that, so it does -- and only falls back to the property
    statement where nothing is running to name.
    """

    def _text(self, frontend):
        with patch(
            "src.utils.firewall.frontend.detect_frontend", return_value=frontend
        ):
            return "\n".join(gateway.open_port_advice(PORT, bridge=BRIDGE, subnet=SUBNET))

    def test_a_detected_front_end_becomes_one_command(self):
        text = self._text(Frontend(name="firewalld", command=f"sudo firewall-cmd --add-port={PORT}/tcp"))
        self.assertIn("firewalld", text)
        self.assertIn(f"sudo firewall-cmd --add-port={PORT}/tcp", text)

    def test_with_no_front_end_it_states_the_property_instead(self):
        text = self._text(None)
        self.assertIn(f"inbound TCP {PORT} accepted", text)
        self.assertIn("input hook", text)
        self.assertIn(SUBNET, text)     # guests
        self.assertIn(BRIDGE, text)

    def test_it_invents_no_command_when_it_detected_nothing(self):
        # Guessing at a front-end that is not running is how an operator ends up
        # configuring the wrong thing, or breaking a ruleset nodo does not own.
        # Naming what it looked for is fine; prescribing a command is not.
        text = self._text(None).lower()
        for tool in ("sudo", "firewall-cmd", "--add-port", "ufw allow", "nft add", "iptables -"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, text)


class OperatorNoticeTests(unittest.TestCase):
    """The block has to stay separate from whatever printed around it.

    These messages are emitted while ConfigManager loads, which on a fresh install
    happens during nodo.py's imports -- so the next thing on the terminal is the KyA
    banner, and the two ran together into one wall of text.
    """

    def test_it_is_padded_with_a_blank_line_at_both_ends(self):
        lines = gateway.operator_notice("gateway port", "body").split("\n")
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[-1], "")

    def test_the_body_is_framed_and_titled(self):
        lines = gateway.operator_notice("gateway port 1 not assigned", "body").split("\n")
        self.assertEqual(lines[1], gateway.NOTICE_RULE)
        self.assertIn("gateway port 1 not assigned", lines[2])
        self.assertEqual(lines[3], gateway.NOTICE_RULE)
        self.assertEqual(lines[4], "body")
        self.assertEqual(lines[5], gateway.NOTICE_RULE)

    def test_a_multi_line_body_is_kept_intact(self):
        # The instructions carry a command on its own indented line; reflowing or
        # re-indenting it here would break the one thing meant to be pasted.
        body = "first\n\n  sudo something --add-port=1/tcp\nlast"
        self.assertIn(body, gateway.operator_notice("t", body))

    def test_the_rule_fits_an_eighty_column_terminal(self):
        self.assertLessEqual(len(gateway.NOTICE_RULE), 79)


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
        self.assertIn("GATEWAY_PORT", instructions)          # the manual way out
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

    @patch("src.utils.firewall.frontend.detect_frontend", return_value=None)
    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_the_refusal_reports_the_chain_it_read(
        self, mock_probe, _frontend, _euid
    ):
        # The rejecting chain is read from the live ruleset, never guessed at, and
        # with no front-end running there is no command to offer.
        mock_probe.return_value = ProbeResult(None, "bridge br-ch does not exist yet")
        self.rejectors = [FIREWALLD_REJECTOR]

        with self.assertRaises(GatewayPortUnavailable) as ctx:
            self._assign()

        instructions = ctx.exception.instructions
        self.assertIn("inet firewalld / filter_INPUT", instructions)  # read, not assumed
        self.assertIn(f"inbound TCP {PORT} accepted", instructions)
        self.assertNotIn("firewall-cmd", instructions)

    @patch(
        "src.utils.firewall.frontend.detect_frontend",
        return_value=Frontend(name="firewalld", command="sudo firewall-cmd --x"),
    )
    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_the_refusal_carries_the_command_for_a_running_front_end(
        self, mock_probe, _frontend, _euid
    ):
        mock_probe.return_value = ProbeResult(None, "bridge br-ch does not exist yet")
        self.rejectors = [FIREWALLD_REJECTOR]

        with self.assertRaises(GatewayPortUnavailable) as ctx:
            self._assign()

        instructions = ctx.exception.instructions
        self.assertIn("This host runs firewalld", instructions)
        self.assertIn("sudo firewall-cmd --x", instructions)
        # Still short enough to read, and to paste somewhere.
        self.assertLess(len(instructions.splitlines()), 16, instructions)


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
