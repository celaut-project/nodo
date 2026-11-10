"""Guests that cannot reach each other because firewalld has no zone for the bridge.

The failure is silent on the node: the gateway answers, the VMs boot, and a service
that launches a dependency reports the child as dead. nodo detected it at start and
wrote one line to ``storage/app.log``. These tests pin that it now reaches the three
places an operator looks (``nodo info``, the TUI, the installer, via a notice file),
that it clears when fixed, and that it is never raised on a guess: a firewalld that
is stopped, or whose answer could not be read, says nothing.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import operator_alerts
from src.utils.firewall import firewalld_bridge as fwd

BRIDGE = "nodo-br-ch"


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _runner(state=None, zone=None):
    """A firewall-cmd whose ``--state`` and ``--get-zone-of-interface`` answers are given."""

    def run(command):
        if command[1] == "--state":
            return state
        return zone

    return run


class BridgeZoneTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fwd.shutil, "which", return_value="/usr/bin/firewall-cmd")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_firewall_cmd_means_nothing_to_report(self):
        with mock.patch.object(fwd.shutil, "which", return_value=None):
            result = fwd.bridge_zone(BRIDGE, run=_runner())
        self.assertFalse(result.running)
        self.assertFalse(result.unzoned)

    def test_a_stopped_firewalld_is_not_what_rejected_the_packet(self):
        result = fwd.bridge_zone(BRIDGE, run=_runner(state=_proc(252, "not running\n")))
        self.assertFalse(result.running)
        self.assertFalse(result.unzoned)

    def test_a_bridge_in_no_zone_is_unzoned(self):
        run = _runner(state=_proc(0, "running\n"), zone=_proc(0, "no zone\n"))
        result = fwd.bridge_zone(BRIDGE, run=run)
        self.assertTrue(result.unzoned)

    def test_no_zone_reported_as_an_error_is_still_unzoned(self):
        run = _runner(state=_proc(0, "running\n"), zone=_proc(1, "", "no zone"))
        self.assertTrue(fwd.bridge_zone(BRIDGE, run=run).unzoned)

    def test_a_bridge_in_a_zone_is_fine(self):
        run = _runner(state=_proc(0, "running\n"), zone=_proc(0, "nodo\n"))
        result = fwd.bridge_zone(BRIDGE, run=run)
        self.assertEqual(result.zone, "nodo")
        self.assertFalse(result.unzoned)

    def test_an_unreadable_answer_is_never_reported_as_unzoned(self):
        run = _runner(state=_proc(0, "running\n"), zone=_proc(2, "", "Authorization failed."))
        result = fwd.bridge_zone(BRIDGE, run=run)
        self.assertFalse(result.readable)
        self.assertFalse(result.unzoned)

    def test_a_runner_that_raises_is_not_reported_as_unzoned(self):
        def boom(command):
            raise OSError("no")

        self.assertFalse(fwd.bridge_zone(BRIDGE, run=boom).unzoned)


class CommandTests(unittest.TestCase):
    def test_the_commands_use_a_zone_of_its_own_and_never_trusted(self):
        commands = fwd.zone_commands(BRIDGE, [52285, 52286])
        text = "\n".join(commands)
        self.assertNotIn("trusted", text)
        self.assertIn(f"--new-zone={fwd.GUEST_ZONE}", text)
        self.assertIn(f"--change-interface={BRIDGE}", text)
        self.assertIn(f"--new-policy={fwd.EGRESS_POLICY}", text)
        self.assertIn("--add-port=52285/tcp", text)
        self.assertIn("--add-port=52286/tcp", text)
        self.assertIn("--add-forward", text)
        self.assertEqual(commands[-1], "sudo firewall-cmd --reload")

    def test_the_one_line_form_chains_every_command(self):
        commands = fwd.zone_commands(BRIDGE, [52285])
        line = fwd.one_line(commands)
        self.assertEqual(line.count(" && "), len(commands) - 1)
        self.assertNotIn("\n", line)


class _FakeManager:
    def __init__(self, config_path):
        self.config_path = config_path


class GuestForwardingAlertTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.config_path = os.path.join(self._dir.name, "config.yaml")
        with open(self.config_path, "w") as handle:
            handle.write("main: {}\n")
        self.manager = _FakeManager(self.config_path)

    def _write(self, name, text):
        from src.utils.config import FORWARD_NOTICE_FILE

        with open(os.path.join(self._dir.name, FORWARD_NOTICE_FILE + name), "w") as handle:
            handle.write(text)

    def test_nothing_pending_is_not_an_alert(self):
        self.assertIsNone(operator_alerts.guest_forwarding_alert(self.manager))

    def test_a_pending_notice_is_an_alert_with_its_command_in_place(self):
        self._write("", "no zone")
        self._write(".cmd", "sudo firewall-cmd --reload")
        alert = operator_alerts.guest_forwarding_alert(self.manager)
        self.assertEqual(alert.key, "guest_forwarding_firewalld")
        self.assertIn("sudo firewall-cmd --reload", alert.summary)
        self.assertTrue(alert.as_line().startswith(operator_alerts.ACTION_REQUIRED))

    def test_without_a_command_it_points_at_the_notice(self):
        self._write("", "no zone")
        alert = operator_alerts.guest_forwarding_alert(self.manager)
        self.assertIn(".forward_notice", alert.summary)

    def test_an_empty_notice_is_not_an_alert(self):
        self._write("", "  \n")
        self.assertIsNone(operator_alerts.guest_forwarding_alert(self.manager))

    def test_it_clears_when_the_notice_is_removed(self):
        self._write("", "no zone")
        self.assertIsNotNone(operator_alerts.guest_forwarding_alert(self.manager))
        os.unlink(os.path.join(self._dir.name, ".forward_notice"))
        self.assertIsNone(operator_alerts.guest_forwarding_alert(self.manager))

    def test_asking_never_spawns_a_process_or_opens_a_socket(self):
        self._write("", "no zone")
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen, \
                mock.patch("socket.socket") as sock:
            operator_alerts.guest_forwarding_alert(self.manager)
        run.assert_not_called()
        popen.assert_not_called()
        sock.assert_not_called()


class ReportFromNetworkSetupTests(unittest.TestCase):
    """``_report_firewalld_zone``: what the daemon does at start."""

    def setUp(self):
        from src.virtualizers.microvm import network

        self.network = network
        self.manager = mock.Mock()
        self.manager.gateway_port_or_none.return_value = 52285
        self.manager.get_plaintext_gateway_port.return_value = 52286
        patcher = mock.patch.object(network, "ConfigManager", return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _zone(self, **kwargs):
        # What the daemon reads now is the whole verdict (zone, forward, egress
        # policy), not only the zone; these keep the original four cases.
        return mock.patch.object(fwd, "assess", return_value=fwd.Assessment(**kwargs))

    def test_an_unzoned_bridge_raises_the_notice_with_both_ports(self):
        with self._zone(running=True, zone=None, effective_zone="public",
                        guest_to_guest=False, egress=True):
            self.network._report_firewalld_zone()
        self.manager.emit_forward_notice.assert_called_once()
        kwargs = self.manager.emit_forward_notice.call_args.kwargs
        self.assertIn("--add-port=52285/tcp", kwargs["command"])
        self.assertIn("--add-port=52286/tcp", kwargs["command"])
        self.manager.clear_forward_notice.assert_not_called()

    def test_a_zoned_bridge_clears_a_stale_notice(self):
        with self._zone(running=True, zone="nodo", effective_zone="nodo",
                        guest_to_guest=True, egress=True):
            self.network._report_firewalld_zone()
        self.manager.emit_forward_notice.assert_not_called()
        self.manager.clear_forward_notice.assert_called_once()

    def test_a_host_without_firewalld_clears_a_stale_notice(self):
        with self._zone(running=False):
            self.network._report_firewalld_zone()
        self.manager.emit_forward_notice.assert_not_called()
        self.manager.clear_forward_notice.assert_called_once()

    def test_an_unreadable_answer_neither_raises_nor_clears(self):
        with self._zone(running=True):
            self.network._report_firewalld_zone()
        self.manager.emit_forward_notice.assert_not_called()
        self.manager.clear_forward_notice.assert_not_called()

    def test_it_is_never_fatal(self):
        with mock.patch.object(fwd, "assess", side_effect=RuntimeError("boom")):
            self.network._report_firewalld_zone()


class CollectOrderTests(unittest.TestCase):
    def test_guest_forwarding_sits_between_the_plaintext_port_and_java(self):
        sentinel = operator_alerts.OperatorAlert(key="guest_forwarding_firewalld", summary="x")
        with mock.patch.object(operator_alerts, "gateway_port_alert", return_value=None), \
                mock.patch.object(operator_alerts, "plaintext_gateway_port_alert", return_value=None), \
                mock.patch.object(operator_alerts, "guest_forwarding_alert", return_value=sentinel), \
                mock.patch.object(operator_alerts, "java_alert",
                                  return_value=operator_alerts.OperatorAlert(key="java_missing", summary="j")):
            keys = [alert.key for alert in operator_alerts.collect(object())]
        self.assertEqual(keys, ["guest_forwarding_firewalld", "java_missing"])


if __name__ == "__main__":
    unittest.main()
