"""The start path is where the gateway port is proven, or the node does not serve.

This node ran for two days with a correct accept rule that a foreign chain on the
same input hook was rejecting: every service it accepted was unable to call back
into the gateway, and nothing noticed, because nothing ever tried the path a guest
takes. So the daemon proves it -- and the two things that used to make that check
vacuous are fixed here:

* The bridge is brought up **before** the probe. It used to be created on the first
  instance launch, so on a node that had never run anything the probe could only
  answer "no bridge yet" -- an inconclusive answer, which only warns.
* The verdict is cached per port per boot, so proving it does not mean rebuilding a
  network namespace on every restart.

A conclusive failure stops the node. An inconclusive one warns and leaves no
marker, so the next start tries again.
"""
import unittest
from unittest.mock import patch

try:
    from src import serve as serve_module

    _IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - bare checkout without config.yaml
    _IMPORT_ERROR = error

from src.utils.firewall.gateway import GatewayPortUnavailable
from src.utils.firewall.reachability import ProbeResult

PORT = 58443
REACHABLE = ProbeResult(True, "connected")
UNKNOWN = ProbeResult(None, "bridge nodo-br-ch does not exist yet")


@unittest.skipIf(_IMPORT_ERROR is not None, f"src.serve unavailable: {_IMPORT_ERROR}")
class VerifyGatewayPortTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def probe(port, verify):
            self.calls.append(("probe", port, verify))
            return self.result

        self.result = REACHABLE
        patcher = patch.object(serve_module, "_gateway_port_call", side_effect=probe)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.env = patch.object(serve_module, "env_manager").start()
        self.addCleanup(patch.stopall)
        self.env.get.return_value = True
        self.env.gateway_port_passed.return_value = False

    def test_an_unproven_port_is_probed_and_then_marked(self):
        serve_module._verify_gateway_port(PORT)

        self.assertEqual(self.calls, [("probe", PORT, True)])
        self.env.mark_gateway_port_passed.assert_called_once_with(PORT)

    def test_a_port_already_proven_in_this_boot_is_not_probed_again(self):
        self.env.gateway_port_passed.return_value = True

        serve_module._verify_gateway_port(PORT)

        self.assertEqual(self.calls, [])
        self.env.mark_gateway_port_passed.assert_not_called()

    def test_verification_turned_off_probes_nothing_and_marks_nothing(self):
        # An operator who has taken the check off does not get a marker claiming the
        # port was proven: nothing proved it.
        self.env.get.return_value = False

        serve_module._verify_gateway_port(PORT)

        self.assertEqual(self.calls, [])
        self.env.mark_gateway_port_passed.assert_not_called()

    def test_an_inconclusive_probe_records_nothing(self):
        # It does not raise, so "it did not fail" is not the test. A marker written on
        # an unknown would be the original bug in a new place: a port nobody proved,
        # never checked again.
        self.result = UNKNOWN

        serve_module._verify_gateway_port(PORT)

        self.assertEqual(self.calls, [("probe", PORT, True)])
        self.env.mark_gateway_port_passed.assert_not_called()

    def test_a_conclusive_failure_stops_the_node_and_marks_nothing(self):
        error = GatewayPortUnavailable("blocked", "open it in firewalld", port=PORT)
        with patch.object(serve_module, "_gateway_port_call", side_effect=error):
            with self.assertRaises(SystemExit) as ctx:
                serve_module._verify_gateway_port(PORT)

        self.assertEqual(ctx.exception.code, 1)
        self.env.mark_gateway_port_passed.assert_not_called()

    def test_the_refusal_is_held_back_to_the_end_of_the_output(self):
        # The start path has already printed a screenful by now, and in a terminal
        # the last line is the one that gets read.
        from src.utils.firewall import gateway

        gateway._DEFERRED_NOTICES.clear()
        self.addCleanup(gateway._DEFERRED_NOTICES.clear)

        error = GatewayPortUnavailable("blocked", "open it in firewalld", port=PORT)
        with patch.object(serve_module, "_gateway_port_call", side_effect=error):
            with patch("builtins.print") as mock_print:
                with self.assertRaises(SystemExit):
                    serve_module._verify_gateway_port(PORT)

        mock_print.assert_not_called()
        self.assertTrue(
            any("open it in firewalld" in notice for notice in gateway._DEFERRED_NOTICES),
            gateway._DEFERRED_NOTICES,
        )


@unittest.skipIf(_IMPORT_ERROR is not None, f"src.serve unavailable: {_IMPORT_ERROR}")
class GuestBridgeTests(unittest.TestCase):
    """The probe needs somewhere to probe from, and it needs it before it runs."""

    def test_the_bridge_is_brought_up_before_the_port_is_touched(self):
        order = []
        with patch.object(
            serve_module, "_ensure_guest_bridge", side_effect=lambda: order.append("bridge")
        ):
            with patch.object(
                serve_module,
                "_open_gateway_port",
                side_effect=lambda port: order.append("port") or (_ for _ in ()).throw(
                    RuntimeError("stop here")
                ),
            ):
                with patch.object(serve_module, "env_manager") as env:
                    env.get_gateway_port.return_value = PORT
                    with self.assertRaises(RuntimeError):
                        serve_module.serve()

        self.assertEqual(order, ["bridge", "port"])

    def test_the_start_path_asks_for_the_assignment_itself(self):
        # It is no longer a side effect of loading the config, so the daemon has to
        # ask -- or a node whose port was never assigned would just fail later with
        # an error about a missing value.
        with patch.object(serve_module, "_ensure_guest_bridge"):
            with patch.object(
                serve_module, "_open_gateway_port", side_effect=RuntimeError("stop here")
            ):
                with patch.object(serve_module, "env_manager") as env:
                    env.get_gateway_port.return_value = PORT
                    with self.assertRaises(RuntimeError):
                        serve_module.serve()

                    env.assign_gateway_port_if_unset.assert_called_once_with()

    def test_an_assignment_that_fails_stops_the_node(self):
        error = GatewayPortUnavailable("no backend", "install nftables", port=PORT)
        with patch.object(serve_module, "env_manager") as env:
            env.assign_gateway_port_if_unset.side_effect = error
            with self.assertRaises(SystemExit):
                serve_module.serve()

    def test_a_bridge_that_cannot_be_created_does_not_stop_the_start(self):
        # A host that cannot make the bridge cannot launch instances either, and that
        # failure belongs to the launch path. Here it only costs an inconclusive
        # probe, which is reported as such.
        logged = []
        with patch(
            "src.virtualizers.ch.execute.ensure_guest_bridge",
            side_effect=OSError("RTNETLINK: operation not permitted"),
        ):
            with patch.object(serve_module.log, "LOGGER", side_effect=logged.append):
                serve_module._ensure_guest_bridge()

        self.assertTrue(any("guest bridge" in message for message in logged), logged)


if __name__ == "__main__":
    unittest.main()
