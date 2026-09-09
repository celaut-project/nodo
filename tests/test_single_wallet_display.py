"""Single-wallet display for nodo info / tx_history (#186 phase 5)."""
import unittest
from unittest import mock

from src.payment_system.contracts import envs


def _contract(ledger="ergo", address="9walletADDR", balance=1.25, cold="9coldADDR",
              unit="ERG"):
    """One registered payment contract, as `print_payment_info` reads it."""
    fake = mock.Mock()
    fake.LEDGER = ledger
    fake.NATIVE_ASSET = unit
    fake.is_demo = False
    fake.get_balance.return_value = (address, balance)
    fake.COLD_WALLET.return_value = cold
    return fake


class PrintPaymentInfoTests(unittest.TestCase):
    def test_prints_one_wallet_line_and_optional_cold_wallet(self):
        with mock.patch.object(envs, "contracts", return_value={"h": _contract()}):
            out = envs.print_payment_info()
        self.assertIn("Wallet: 9walletADDR, Amount: 1.25 ERG", out)
        self.assertIn("Cold Wallet: 9coldADDR", out)
        # No trace of the old two-wallet vocabulary.
        self.assertNotIn("Sending Wallet", out)
        self.assertNotIn("Receiver Wallet", out)
        self.assertNotIn("Total:", out)

    def test_omits_cold_wallet_line_when_unset(self):
        with mock.patch.object(
            envs, "contracts", return_value={"h": _contract(balance=0.0, cold="")}
        ):
            out = envs.print_payment_info()
        self.assertIn("Wallet: 9walletADDR", out)
        self.assertNotIn("Cold Wallet:", out)

    def test_one_block_per_contract_and_never_a_total(self):
        """Two payment systems are two balances in two places.

        Adding them up would name a figure the operator cannot spend: they are held on
        different chains, in different money, and only one of them can pay any given
        peer.
        """
        with mock.patch.object(envs, "contracts", return_value={
            "h1": _contract(ledger="ergo", address="9erg", balance=1.25, unit="ERG"),
            "h2": _contract(ledger="bitcoin", address="bc1q", balance=0.5, cold="",
                            unit="BTC"),
        }):
            out = envs.print_payment_info()
        self.assertIn("ergo: Wallet: 9erg, Amount: 1.25 ERG", out)
        self.assertIn("bitcoin: Wallet: bc1q, Amount: 0.5 BTC", out)
        self.assertNotIn("Total:", out)

    def test_a_node_nobody_can_pay_says_so_rather_than_printing_nothing(self):
        # An empty string would read as "no wallet configured yet" on a node whose
        # payment stack simply failed to load.
        with mock.patch.object(envs, "contracts", return_value={}):
            out = envs.print_payment_info()
        self.assertIn("No payment system is available", out)

    def test_a_wallet_that_cannot_be_read_is_named_rather_than_omitted(self):
        broken = _contract()
        broken.get_balance.side_effect = RuntimeError("node unreachable")
        with mock.patch.object(envs, "contracts", return_value={"h": broken}):
            out = envs.print_payment_info()
        self.assertIn("wallet unavailable", out)
        self.assertIn("node unreachable", out)


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
