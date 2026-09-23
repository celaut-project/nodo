"""The plaintext gateway port's counterpart of test_serve_gateway_verification.py.

Same probe, same guest subnet, same "an accept rule is not the same as reachable"
failure mode -- but a different audience and a different consequence. The TLS port
is what peers and the CLI dial, so an unreachable TLS port means the node is not
serving anybody, and the daemon refuses to start over it. The plaintext port is
never announced to a peer at all (docs/FIREWALL.md); it exists so the services
this node launches can call back into it. So a conclusive failure here is worth an
operator alert -- every microVM the node runs from now on is handed an address
that answers nothing -- but never worth stopping the node: peers keep working
through the TLS port regardless.
"""
import unittest
import unittest.mock
from unittest.mock import patch

try:
    from src import serve as serve_module

    _IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - bare checkout without config.yaml
    _IMPORT_ERROR = error

from src.utils.firewall.reachability import ProbeResult

PORT = 58444
REACHABLE = ProbeResult(True, "connected")
UNKNOWN = ProbeResult(None, "bridge nodo-br-ch does not exist yet")
BLOCKED = ProbeResult(False, "connection refused")


@unittest.skipIf(_IMPORT_ERROR is not None, f"src.serve unavailable: {_IMPORT_ERROR}")
class VerifyPlaintextGatewayPortTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def probe(*, bridge, target_ip, port, subnet):
            self.calls.append((bridge, target_ip, port, subnet))
            return self.result

        self.result = REACHABLE
        patcher = patch.object(serve_module, "probe_tcp_from_bridge", side_effect=probe)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.env = patch.object(serve_module, "env_manager").start()
        self.addCleanup(patch.stopall)
        self.verify_reachability = True

        def get(key, default=None):
            if key == "network.VERIFY_GATEWAY_REACHABILITY":
                return self.verify_reachability
            return default

        self.env.get.side_effect = get
        self.env.plaintext_gateway_port_passed.return_value = False

    def test_a_falsy_port_is_never_probed(self):
        # 0 (or None) means the operator disabled it; services fall back to the
        # TLS port, which is a deliberate configuration, not something to check.
        serve_module._verify_plaintext_gateway_port(0)

        self.assertEqual(self.calls, [])
        self.env.plaintext_gateway_port_passed.assert_not_called()

    def test_an_unproven_port_is_probed_and_then_marked(self):
        serve_module._verify_plaintext_gateway_port(PORT)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], PORT)
        self.env.mark_plaintext_gateway_port_passed.assert_called_once_with(PORT)

    def test_a_port_already_proven_in_this_boot_is_not_probed_again(self):
        self.env.plaintext_gateway_port_passed.return_value = True

        serve_module._verify_plaintext_gateway_port(PORT)

        self.assertEqual(self.calls, [])
        self.env.mark_plaintext_gateway_port_passed.assert_not_called()

    def test_verification_turned_off_probes_nothing_and_marks_nothing(self):
        self.verify_reachability = False

        serve_module._verify_plaintext_gateway_port(PORT)

        self.assertEqual(self.calls, [])
        self.env.mark_plaintext_gateway_port_passed.assert_not_called()

    def test_an_inconclusive_probe_records_and_alerts_nothing(self):
        self.result = UNKNOWN

        serve_module._verify_plaintext_gateway_port(PORT)

        self.assertEqual(len(self.calls), 1)
        self.env.mark_plaintext_gateway_port_passed.assert_not_called()
        self.env.emit_plaintext_gateway_notice.assert_not_called()

    def test_a_conclusive_failure_never_stops_the_node(self):
        """The whole point of this check: unreachable here is an alert, not a refusal.

        Peers and the CLI never use this port, so nothing about the node's ability
        to serve them depends on it.
        """
        self.result = BLOCKED

        try:
            serve_module._verify_plaintext_gateway_port(PORT)
        except SystemExit:
            self.fail("an unreachable plaintext gateway port must not stop the node")

        self.env.mark_plaintext_gateway_port_passed.assert_not_called()
        self.env.emit_plaintext_gateway_notice.assert_called_once()
        title, body = self.env.emit_plaintext_gateway_notice.call_args[0]
        self.assertIn(str(PORT), body)
        # Guest subnet only, never the wide-open form the TLS port's own advice
        # would produce -- this port is unauthenticated plain gRPC.
        self.assertIn("192.168.200.0/24", body)

    def test_a_detected_front_ends_command_travels_with_the_notice(self):
        """So `nodo info` can show it in place instead of pointing at the notice file."""
        from src.utils.firewall.frontend import Frontend

        self.result = BLOCKED
        with patch(
            "src.utils.firewall.frontend.detect_scoped_frontend",
            return_value=Frontend(
                "ufw", "sudo ufw allow from 192.168.200.0/24 to any port 58444 proto tcp"
            ),
        ):
            serve_module._verify_plaintext_gateway_port(PORT)

        self.assertEqual(
            self.env.emit_plaintext_gateway_notice.call_args.kwargs.get("command"),
            "sudo ufw allow from 192.168.200.0/24 to any port 58444 proto tcp",
        )

    def test_no_detected_front_end_means_no_command_on_the_notice(self):
        self.result = BLOCKED
        with patch("src.utils.firewall.frontend.detect_scoped_frontend", return_value=None):
            serve_module._verify_plaintext_gateway_port(PORT)

        self.assertIsNone(
            self.env.emit_plaintext_gateway_notice.call_args.kwargs.get("command")
        )


@unittest.skipIf(_IMPORT_ERROR is not None, f"src.serve unavailable: {_IMPORT_ERROR}")
class VerifyGatewayPortsTests(unittest.TestCase):
    """Both ports are probed, whatever the TLS port's verdict.

    The plaintext probe used to run only after the TLS one passed, so a firewall
    blocking both (the usual case: one INPUT policy covers every port) refused the
    start over the TLS port and never wrote the plaintext alert -- `nodo info` and the
    TUI showed one problem while the node had two.
    """

    def setUp(self):
        self.order = []
        self.server = unittest.mock.Mock()
        self.server.stop.side_effect = lambda grace: self.order.append("stop")
        self.plaintext = patch.object(
            serve_module,
            "_verify_plaintext_gateway_port",
            side_effect=lambda port: self.order.append(("plaintext", port)),
        ).start()
        self.addCleanup(patch.stopall)

    def test_a_refused_start_still_probes_the_plaintext_port_before_stopping(self):
        patch.object(
            serve_module, "_verify_gateway_port", side_effect=SystemExit(1)
        ).start()

        with self.assertRaises(SystemExit):
            serve_module._verify_gateway_ports(self.server, PORT, PORT + 1)

        # Before stop(): the probe needs the daemon's own listener to still be up.
        self.assertEqual(self.order, [("plaintext", PORT + 1), "stop"])

    def test_a_plaintext_probe_that_raises_never_replaces_the_refusal(self):
        patch.object(
            serve_module, "_verify_gateway_port", side_effect=SystemExit(1)
        ).start()
        self.plaintext.side_effect = RuntimeError("boom")
        patch.object(serve_module.log, "LOGGER").start()

        with self.assertRaises(SystemExit):
            serve_module._verify_gateway_ports(self.server, PORT, PORT + 1)

        self.server.stop.assert_called_once_with(0)

    def test_a_reachable_tls_port_leaves_the_server_running(self):
        patch.object(serve_module, "_verify_gateway_port").start()

        serve_module._verify_gateway_ports(self.server, PORT, PORT + 1)

        self.assertEqual(self.order, [("plaintext", PORT + 1)])
        self.server.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
