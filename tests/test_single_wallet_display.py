"""Single-wallet display for nodo info / tx_history (#186 phase 5)."""
import unittest
from unittest import mock

from src.payment_system.contracts import envs


class PrintPaymentInfoTests(unittest.TestCase):
    def test_prints_one_wallet_line_and_optional_cold_wallet(self):
        fake = mock.Mock()
        fake.get_balance.return_value = ("9walletADDR", 1.25)
        fake.COLD_WALLET.return_value = "9coldADDR"
        with mock.patch.object(envs, "_ergo_interface", return_value=fake):
            out = envs.print_payment_info()
        self.assertIn("Wallet: 9walletADDR, Amount: 1.25 ERGs", out)
        self.assertIn("Cold Wallet: 9coldADDR", out)
        # No trace of the old two-wallet vocabulary.
        self.assertNotIn("Sending Wallet", out)
        self.assertNotIn("Receiver Wallet", out)
        self.assertNotIn("Total:", out)

    def test_omits_cold_wallet_line_when_unset(self):
        fake = mock.Mock()
        fake.get_balance.return_value = ("9walletADDR", 0.0)
        fake.COLD_WALLET.return_value = ""
        with mock.patch.object(envs, "_ergo_interface", return_value=fake):
            out = envs.print_payment_info()
        self.assertIn("Wallet: 9walletADDR", out)
        self.assertNotIn("Cold Wallet:", out)


class TxHistorySingleWalletTests(unittest.TestCase):
    def test_uses_single_wallet_address(self):
        import src.commands.tx_history as th
        with mock.patch(
            "src.payment_system.contracts.ergo.interface.get_wallet_address",
            return_value="9walletADDR",
        ):
            self.assertEqual(th._get_wallet_address(), "9walletADDR")

    def test_only_one_wallet_section_rendered(self):
        import src.commands.tx_history as th
        calls = []
        with mock.patch.object(th, "_get_wallet_address", return_value="9walletADDR"), \
             mock.patch.object(th, "_display_wallet_transactions", side_effect=lambda label, addr: calls.append(label)):
            th.tx_history()
        self.assertEqual(calls, ["Wallet"])  # exactly one wallet card, labelled "Wallet"


if __name__ == "__main__":
    unittest.main()
