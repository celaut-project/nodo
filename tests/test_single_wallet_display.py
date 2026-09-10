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


class TxHistoryTests(unittest.TestCase):
    """One section per payment system, and no chain named in the command.

    Every chain-shaped part of this used to live in the command: it read the Ergo
    explorer, walked Ergo boxes to decide a direction, and reached into
    `contracts.ergo.interface`'s privates to render nanoERG -- so "transaction
    history" meant "Ergo's", and a second payment system had nowhere to appear.
    """

    def _rendered(self, *contracts_):
        import io as _io
        from contextlib import redirect_stdout

        import src.commands.tx_history as th

        buf = _io.StringIO()
        with mock.patch("src.payment_system.contracts.registry.contracts",
                        return_value={c.CONTRACT_HASH: c for c in contracts_}), \
                mock.patch.object(th, "_payments_by_tx_id", return_value={}), \
                mock.patch.object(th, "_clients_by_deposit_token", return_value={}), \
                redirect_stdout(buf):
            th.tx_history()
        return buf.getvalue()

    @staticmethod
    def _history_contract(ledger="ergo", address="9walletADDR", rows=None, unit="ERG",
                          decimals=9):
        contract = mock.Mock()
        contract.LEDGER = ledger
        contract.CONTRACT_HASH = f"hash-{ledger}"
        contract.is_demo = False
        contract.get_wallet_address.return_value = address
        contract.transaction_history.return_value = rows if rows is not None else [{
            "id": "tx-1", "timestamp": 1_760_000_000, "confirmations": 3,
            "direction": "in", "amount": 1_500_000_000, "unit": unit,
            "decimals": decimals, "counterparties": ["9payerADDR"],
            "deposit_tokens": [],
        }]
        return contract

    def test_one_section_per_payment_system(self):
        out = self._rendered(
            self._history_contract(),
            self._history_contract(ledger="bitcoin", address="bc1qxyz", rows=[{
                "id": "tx-2", "timestamp": 1_760_000_100, "confirmations": 1,
                "direction": "out", "amount": 50_000, "unit": "BTC", "decimals": 8,
                "counterparties": ["bc1qpeer"], "deposit_tokens": [],
            }]),
        )
        self.assertIn("[ergo] - Address: 9walletADDR", out)
        self.assertIn("[bitcoin] - Address: bc1qxyz", out)

    def test_each_amount_is_rendered_in_its_own_money(self):
        out = self._rendered(
            self._history_contract(),
            self._history_contract(ledger="bitcoin", address="bc1qxyz", rows=[{
                "id": "tx-2", "timestamp": 1_760_000_100, "confirmations": 1,
                "direction": "out", "amount": 50_000, "unit": "BTC", "decimals": 8,
                "counterparties": [], "deposit_tokens": [],
            }]),
        )
        self.assertIn("1.500000000 ERG", out)
        self.assertIn("0.00050000 BTC", out)

    def test_a_timestamp_is_read_as_seconds(self):
        # Each contract normalises its own chain's units; a page that divided by a
        # thousand on behalf of one chain dated the other one to 1970.
        out = self._rendered(self._history_contract())
        self.assertNotIn("1970", out)
        self.assertNotIn("N/A", out)

    def test_a_system_that_cannot_be_read_says_so_rather_than_showing_nothing(self):
        # An empty history reads as "this wallet was never used", which is a different
        # claim from "the explorer did not answer".
        broken = self._history_contract()
        broken.transaction_history.side_effect = ValueError("explorer unreachable")
        out = self._rendered(broken)
        self.assertIn("Could not read the ergo history", out)
        self.assertIn("explorer unreachable", out)

    def test_a_node_with_no_payment_system_says_so(self):
        self.assertIn("No payment system can report a history", self._rendered())

    def test_a_contract_that_reports_no_history_is_skipped(self):
        # The simulated contract has no chain to report one from.
        no_history = mock.Mock(spec=["LEDGER", "CONTRACT_HASH", "is_demo"])
        no_history.LEDGER, no_history.CONTRACT_HASH = "simulated", "hash-sim"
        no_history.is_demo = False
        self.assertIn("No payment system can report a history",
                      self._rendered(no_history))


if __name__ == "__main__":
    unittest.main()
