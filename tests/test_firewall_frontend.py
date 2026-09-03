"""Naming the firewall front-end that is actually running, and nothing else.

The message an operator gets when the gateway port is blocked used to state a
property ("TCP <port> must be accepted on the input hook, and no other base chain
may reject it") and deliberately refuse to name a tool. Correct, and unusable: the
operator still had to work out which of firewalld/ufw/nothing owns their host.

So nodo reads that off the host -- binary present AND reporting itself active -- and
prints the one command for it. What it must never do is guess: an installed but
stopped firewalld is not what is rejecting the packet, and sending someone to
configure it (or to hand-edit a ruleset nodo does not own) makes things worse than
saying nothing.
"""
import subprocess
import unittest
from unittest.mock import patch

from src.utils.firewall import frontend as fe


def _proc(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class DetectFrontendTests(unittest.TestCase):
    def _detect(self, present, outputs):
        def which(name):
            return f"/usr/sbin/{name}" if name in present else None

        def run(command):
            return _proc(outputs.get(command[0], ""))

        with patch.object(fe.shutil, "which", side_effect=which):
            return fe.detect_frontend(58443, run=run)

    def test_running_firewalld_is_named_with_a_single_command(self):
        found = self._detect({"firewall-cmd"}, {"firewall-cmd": "running\n"})
        self.assertEqual(found.name, "firewalld")
        self.assertIn("--add-port=58443/tcp", found.command)
        # One paste, not a procedure: permanent rule plus the reload it needs.
        self.assertIn("--reload", found.command)

    def test_a_stopped_firewalld_is_not_named(self):
        # 'not running' means something else is rejecting the packet; pointing the
        # operator at firewalld would send them after the wrong thing.
        self.assertIsNone(self._detect({"firewall-cmd"}, {"firewall-cmd": "not running\n"}))

    def test_active_ufw_is_named(self):
        found = self._detect({"ufw"}, {"ufw": "Status: active\n"})
        self.assertEqual(found.name, "ufw")
        self.assertEqual(found.command, "sudo ufw allow 58443/tcp")

    def test_inactive_ufw_is_not_named(self):
        self.assertIsNone(self._detect({"ufw"}, {"ufw": "Status: inactive\n"}))

    def test_an_absent_binary_is_not_probed(self):
        self.assertIsNone(self._detect(set(), {}))

    def test_a_failing_probe_never_raises(self):
        def boom(_command):
            raise OSError("no such tool")

        with patch.object(fe.shutil, "which", return_value="/usr/sbin/ufw"):
            self.assertIsNone(fe.detect_frontend(58443, run=boom))


class OpenPortAdviceTests(unittest.TestCase):
    """Two lines with a command, or one short paragraph. Never a page."""

    def test_it_is_the_command_when_a_front_end_was_detected(self):
        with patch.object(
            fe, "detect_frontend", return_value=fe.Frontend("ufw", "sudo ufw allow 1/tcp")
        ):
            lines = fe.open_port_advice(1, bridge="nodo-br-ch", subnet="10.0.0.0/24")
        self.assertEqual(len(lines), 2)
        self.assertIn("sudo ufw allow 1/tcp", lines[1])

    def test_it_states_the_property_when_nothing_was_detected(self):
        with patch.object(fe, "detect_frontend", return_value=None):
            lines = fe.open_port_advice(58443, bridge="nodo-br-ch", subnet="10.0.0.0/24")
        text = "\n".join(lines)
        self.assertIn("inbound TCP 58443 accepted", text)
        self.assertIn("nodo-br-ch", text)
        self.assertIn("10.0.0.0/24", text)
        # Short, and wrapped: this gets printed into a terminal and pasted around.
        self.assertLessEqual(len(lines), 6, text)
        self.assertTrue(all(len(line) <= 78 for line in lines), lines)

    def test_the_guest_network_is_omitted_when_it_is_unknown(self):
        with patch.object(fe, "detect_frontend", return_value=None):
            text = "\n".join(fe.open_port_advice(58443))
        self.assertNotIn("Guests reach", text)


if __name__ == "__main__":
    unittest.main()
