"""`nodo doctor` checks the plaintext gateway port, not only the TLS one.

Services are handed network.GATEWAY_PLAINTEXT_PORT (``auto`` = GATEWAY_PORT + 1) in
their __config__.gateway; the TLS port is what peers dial. A firewall can pass one
and drop the other, so a doctor that probed only the TLS port reported the peers'
path and said nothing about the one every guest actually uses.
"""
import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

from src.commands import doctor
from src.utils.firewall.reachability import ProbeResult

TLS_PORT = 52285


class DoctorPlaintextGatewayTests(unittest.TestCase):
    def setUp(self):
        self.manager = MagicMock()
        self.manager.gateway_port_or_none.return_value = TLS_PORT
        self.manager.get_plaintext_gateway_port.return_value = TLS_PORT + 1
        self.manager.get.side_effect = lambda key, default=None: default
        patch("src.utils.config.ConfigManager", return_value=self.manager).start()

        self.probed = []
        self.result = ProbeResult(True, "connected")

        def probe(**kwargs):
            self.probed.append(kwargs["port"])
            return self.result

        patch("src.utils.firewall.reachability.probe_tcp_from_bridge", side_effect=probe).start()
        backend = MagicMock()
        backend.foreign_input_rejectors.return_value.describe.return_value = [
            "ip filter / INPUT: chain policy is drop"
        ]
        backend.foreign_input_rejectors.return_value.rejectors = ["INPUT"]
        patch("src.utils.firewall.backends.detect_backend", return_value=backend).start()
        self.advice = patch(
            "src.utils.firewall.frontend.open_scoped_port_advice",
            return_value=["This host runs ufw. Open the port, admitting only 192.168.200.0/24, with:",
                          "  sudo ufw allow from 192.168.200.0/24 to any port 52286 proto tcp"],
        ).start()
        self.addCleanup(patch.stopall)

    def run_check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor._doctor_guest_plaintext_gateway_reachability()
        return out.getvalue()

    def test_the_port_services_are_handed_is_the_one_probed(self):
        output = self.run_check()

        self.assertEqual(self.probed, [TLS_PORT + 1])
        self.assertIn("[OK] A guest on nodo-br-ch can reach 192.168.200.1:52286", output)

    def test_an_unreachable_port_fails_with_advice_scoped_to_the_guest_subnet(self):
        self.result = ProbeResult(False, "timed out")

        output = self.run_check()

        self.assertIn("[FAIL]", output)
        self.assertIn("52286", output)
        self.assertIn("chain policy is drop", output)
        self.assertIn("Suggestion:", output)
        self.advice.assert_called_once_with(
            TLS_PORT + 1, subnet="192.168.200.0/24", bridge="nodo-br-ch"
        )

    def test_a_disabled_plaintext_port_is_not_probed(self):
        self.manager.get_plaintext_gateway_port.return_value = 0

        output = self.run_check()

        self.assertEqual(self.probed, [])
        self.assertIn("disabled", output)

    def test_an_unassigned_tls_port_is_not_probed(self):
        self.manager.gateway_port_or_none.return_value = None

        output = self.run_check()

        self.assertEqual(self.probed, [])
        self.manager.get_plaintext_gateway_port.assert_not_called()
        self.assertIn("[WARN]", output)


if __name__ == "__main__":
    unittest.main()
