"""Who decides that the gateway port is usable, and when.

The incident: a host picked a port, wrote it down, opened it in nodo's own nftables
table, and started serving. Another ruleset on the same input hook rejected it, so
peers on the same LAN got "no route to host" while the node looked healthy from the
inside. nodo's accept rule was correct and irrelevant -- in nftables 'accept' ends
the evaluation of its own chain only, and a reject elsewhere on the hook wins
regardless of priority.

The first fix put the check at *assignment* time: probe the port with a throwaway
listener, and refuse to store one that could not be cleared. It made things worse
in a way worth remembering. Nothing can be probed before the guest bridge exists,
and the bridge was created on the first instance launch -- so on a host with
firewalld and no bridge yet, every assignment was refused, forever. The only move
left to the operator was to pin a port by hand, and a hand-pinned port went through
no check at all. The guard did not prevent the bad state; it routed people around
itself into the one path with no verification on it.

So the decision moved instead of being strengthened:

* Assignment opens the port in nodo's ruleset and stores it. It does not probe --
  see ``assign_gateway_port``.
* The daemon's start path creates the bridge first, then proves the port, and
  refuses to serve on one that is provably unreachable. Once per port per boot;
  the verdict is cached (``src/utils/config.py``).
* A refusal takes nodo's accept rule back out. Opening a port for a node that then
  declines to answer on it leaves a hole, not a leftover.
* The alert is deferred to the end of the process, because in a terminal the last
  thing printed is the first thing read.

nodo still does not drive the firewall for the operator: it writes nftables (or
iptables) rules and reads the ruleset back. But the failure message names the
front-end that is *running on this host* -- detected, not assumed -- and gives the
single command that opens the port with it, falling back to a one-paragraph
statement of what has to hold where there is no front-end to name.
"""
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.utils.firewall.backends import ForeignRejector, NftBackend, RejectorScan
from src.utils.firewall.errors import FirewallError
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


class _FakeBackend(NftBackend):
    """An nft backend that records instead of touching the host."""

    def __init__(self, rejectors=()):
        super().__init__(run=FakeRunner())
        self.opened = []
        self.withdrawn = []
        self.rejectors = list(rejectors)

    name = "nftables"

    def ensure_input_accept(self, port, protocol, comment):
        self.opened.append((port, protocol, comment))
        return True

    def prune_input_accepts(self, comment_prefix, keep):
        return []

    def delete_by_comment(self, chain, comment):
        self.withdrawn.append(comment)
        return 1

    def list_input_accepts(self, comment_prefix):
        return []

    def foreign_input_rejectors(self):
        return RejectorScan(rejectors=tuple(self.rejectors))


@patch("src.utils.firewall.gateway.os.geteuid", return_value=0)
class AssignGatewayPortTests(unittest.TestCase):
    """Assignment claims the port. It does not adjudicate reachability."""

    def setUp(self):
        self.backend = _FakeBackend()
        self.logged = []
        patcher = patch(
            "src.utils.firewall.gateway.detect_backend", return_value=self.backend
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assign(self):
        return assign_gateway_port(
            port=PORT, config_path="/nodo/config.yaml", log=self.logged.append
        )

    def test_the_port_is_opened_in_nodos_own_ruleset(self, _euid):
        self._assign()
        self.assertEqual(
            self.backend.opened, [(PORT, "tcp", gateway.gateway_comment(PORT))]
        )

    @patch("src.utils.firewall.gateway.probe_tcp_from_bridge")
    def test_it_does_not_probe(self, mock_probe, _euid):
        # There is nothing to probe from yet: assignment runs while the config
        # loads, before the guest bridge exists. A probe that cannot run is not a
        # verdict, and treating it as one is what made this unassignable.
        self._assign()
        mock_probe.assert_not_called()

    def test_a_host_that_can_reject_no_longer_blocks_assignment(self, _euid):
        # The Fedora dead end: firewalld on the input hook, no bridge to probe from.
        # This used to refuse, every time, so the port was never stored and the
        # operator's only way forward was an unverified manual pin.
        self.backend.rejectors = [FIREWALLD_REJECTOR]
        self._assign()
        self.assertEqual(len(self.backend.opened), 1)

    def test_a_rule_that_cannot_be_applied_still_refuses(self, _euid):
        # Nothing was opened, so there is nothing to store: this is the one failure
        # assignment still owns.
        def explode(*_args, **_kwargs):
            raise FirewallError("nft: permission denied")

        self.backend.ensure_input_accept = explode
        with self.assertRaises(GatewayPortUnavailable) as ctx:
            self._assign()
        self.assertEqual(ctx.exception.port, PORT)

    def test_it_needs_root(self, _euid):
        with patch("src.utils.firewall.gateway.os.geteuid", return_value=1000):
            with self.assertRaises(GatewayPortUnavailable) as ctx:
                self._assign()
        self.assertIn("root", str(ctx.exception).lower())


@patch("src.utils.firewall.gateway.os.geteuid", return_value=0)
class RefusalWithdrawsTheRuleTests(unittest.TestCase):
    """A port nodo will not answer on does not stay open in the host's ruleset.

    ``ensure_gateway_port_open`` applies the accept rule before it can probe --
    there is no other order available, the probe needs the rule under test. When
    the probe then comes back conclusively negative and the node refuses to start,
    that rule is left pointing at a port nothing will ever answer on. The next
    start re-applies it, so nothing is lost by taking it out, and a hole nobody
    uses is not something to leave behind.
    """

    def setUp(self):
        self.backend = _FakeBackend()
        self.logged = []

    def _open(self, probe):
        with patch(
            "src.utils.firewall.gateway.probe_tcp_from_bridge", return_value=probe
        ):
            return gateway.ensure_gateway_port_open(
                port=PORT,
                bridge=BRIDGE,
                gateway_ip=GATEWAY_IP,
                subnet=SUBNET,
                backend=self.backend,
                verify=True,
                strict=True,
                log=self.logged.append,
            )

    def test_a_blocked_port_is_refused_and_its_rule_removed(self, _euid):
        with self.assertRaises(GatewayPortUnavailable):
            self._open(ProbeResult(False, "connect refused"))
        self.assertEqual(self.backend.withdrawn, [gateway.gateway_comment(PORT)])

    def test_a_reachable_port_keeps_its_rule(self, _euid):
        self._open(ProbeResult(True, "connected"))
        self.assertEqual(self.backend.withdrawn, [])

    def test_an_inconclusive_probe_keeps_its_rule(self, _euid):
        # Not a verdict, so not a refusal, so nothing to undo. The node warns and
        # runs; the port simply stays unmarked and is probed again next start.
        self._open(ProbeResult(None, "bridge nodo-br-ch does not exist yet"))
        self.assertEqual(self.backend.withdrawn, [])

    def test_a_failed_withdrawal_does_not_mask_the_refusal(self, _euid):
        # The operator needs the instructions far more than they need the rule gone.
        def explode(*_args, **_kwargs):
            raise FirewallError("nft: cannot delete")

        self.backend.delete_by_comment = explode
        with self.assertRaises(GatewayPortUnavailable):
            self._open(ProbeResult(False, "connect refused"))


class DeferredNoticeTests(unittest.TestCase):
    """The alert has to be the last thing on the terminal, not the middle.

    These are emitted while ConfigManager loads -- on a fresh install, during
    nodo.py's imports -- and after an install the operator is looking at pages of
    completion, chown and systemd output. A message in the middle of that is a
    message nobody acts on.
    """

    def setUp(self):
        gateway._DEFERRED_NOTICES.clear()
        self.addCleanup(gateway._DEFERRED_NOTICES.clear)

    def test_deferring_prints_nothing_yet(self):
        with patch("builtins.print") as mock_print:
            gateway.defer_operator_notice("open the port")
        mock_print.assert_not_called()
        self.assertEqual(gateway._DEFERRED_NOTICES, ["open the port"])

    def test_flushing_writes_them_to_stderr_and_empties_the_queue(self):
        gateway.defer_operator_notice("first")
        gateway.defer_operator_notice("second")
        with patch("builtins.print") as mock_print:
            gateway.flush_operator_notices()
        self.assertEqual(
            [call.args[0] for call in mock_print.call_args_list], ["first", "second"]
        )
        self.assertTrue(all(call.kwargs["file"] is sys.stderr for call in mock_print.call_args_list))
        self.assertEqual(gateway._DEFERRED_NOTICES, [])

    def test_the_same_notice_twice_is_one_alert(self):
        # ConfigManager can load more than once in a process; the operator has one
        # thing to do about it either way.
        gateway.defer_operator_notice("open the port")
        gateway.defer_operator_notice("open the port")
        self.assertEqual(len(gateway._DEFERRED_NOTICES), 1)


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
