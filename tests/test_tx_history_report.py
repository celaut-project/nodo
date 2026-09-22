"""`nodo tx_history --json` is what the TUI's payment-systems block reads.

The printed command and the report are one computation, for the same reason
`nodo reputation` and `nodo donations` are: a second walk of the same chains would be a
second answer to "is this ledger reachable", and the two would disagree on exactly the
day an operator went looking.

What the report has to get right is the part a printed page gets for free: **states**.
A ledger nobody configured, a ledger configured with no usable rate, and a ledger that
is offered but whose explorer just timed out are three different facts with three
different fixes, and every one of them has to survive as data rather than as an
exception. So these tests are mostly about what the report says when something is
wrong.
"""
import unittest
from unittest import mock

from tests.config_bootstrap import load_example_config

load_example_config()

import src.commands.tx_history as tx_history  # noqa: E402
from src.payment_system.contracts.registry import _Candidate  # noqa: E402

OURS = "9ourWALLETaddress"
THEIRS = "9theirCONTRACTaddress"


def _row(tx_id="tx-1", direction="in", amount=1_500_000_000):
    """One transaction as a payment contract reports it, chain-shape already resolved."""
    return {
        "id": tx_id,
        "timestamp": 1_700_000_000,
        "confirmations": 12,
        "direction": direction,
        "amount": amount,
        "unit": "ERG",
        "decimals": 9,
        "counterparties": [THEIRS],
        "deposit_tokens": [],
    }


class _Contract:
    """The smallest thing `report` treats as an offered payment system."""

    LEDGER = "ergo"

    def __init__(self, rows=(), raises=None, address=OURS):
        self._rows = list(rows)
        self._raises = raises
        self._address = address

    def get_wallet_address(self):
        if isinstance(self._address, Exception):
            raise self._address
        return self._address

    def transaction_history(self, limit=10):
        if self._raises:
            raise self._raises
        return self._rows[:limit]


def _report(contracts, *, configured=("ergo", "bitcoin"), candidates=None, limit=None):
    """`report()` with the registry answering exactly what a test wants it to."""
    candidates = candidates or (
        _Candidate("simulated", "src.payment_system.contracts.simulator.interface"),
        _Candidate(
            "ergo",
            "src.payment_system.contracts.ergo.interface",
            "src.payment_system.contracts.ergo.rate",
        ),
        _Candidate(
            "bitcoin",
            "src.payment_system.contracts.bitcoin.interface",
            "src.payment_system.contracts.bitcoin.rate",
        ),
    )
    with mock.patch(
        "src.payment_system.contracts.registry.contracts",
        return_value={getattr(c, "CONTRACT_HASH", str(i)): c
                      for i, c in enumerate(contracts)},
    ), mock.patch(
        "src.payment_system.contracts.registry.is_configured",
        side_effect=lambda name: name in configured,
    ), mock.patch(
        "src.payment_system.contracts.registry.CANDIDATES", candidates
    ), mock.patch.object(
        tx_history, "_clients_by_deposit_token", return_value={}
    ), mock.patch.object(
        tx_history, "_payments_by_tx_id", return_value={}
    ):
        if limit is None:
            return tx_history.report(now=1_700_000_100)
        return tx_history.report(limit=limit, now=1_700_000_100)


def _ledger(data, name):
    return next(entry for entry in data["ledgers"] if entry["ledger"] == name)


class TheReportCoversEveryLedgerTests(unittest.TestCase):
    """Every candidate appears, offered or not."""

    def test_the_simulator_is_not_a_payment_system_an_operator_configures(self):
        # It is a flag, not a ledger, and showing it beside Ergo would invite an
        # operator to read simulated money as money.
        data = _report([])

        self.assertNotIn("simulated", [entry["ledger"] for entry in data["ledgers"]])

    def test_a_ledger_that_is_not_offered_still_gets_an_entry(self):
        # The whole reason the report is per candidate: "where did Bitcoin go" is a
        # question a list of what works cannot answer.
        data = _report([])

        self.assertEqual(
            [entry["ledger"] for entry in data["ledgers"]], ["ergo", "bitcoin"]
        )

    def test_configured_and_offered_are_reported_apart(self):
        """Two different failures, and only one of them is the operator's to fix.

        `ledgers.bitcoin` present with an unusable rate is a misconfiguration; no
        `ledgers.bitcoin` at all is a node that was never asked to do Bitcoin.
        """
        data = _report([_Contract()], configured=("ergo",))

        self.assertTrue(_ledger(data, "ergo")["offered"])
        bitcoin = _ledger(data, "bitcoin")
        self.assertFalse(bitcoin["configured"])
        self.assertFalse(bitcoin["offered"])
        self.assertIn("not configured", bitcoin["unavailable_reason"])

    def test_a_configured_ledger_that_is_not_offered_carries_the_contracts_own_reason(self):
        # The sentence the registry logs, which names the key to fix -- rather than a
        # second wording of it written here.
        with mock.patch(
            "src.payment_system.contracts.bitcoin.interface.unavailable_reason",
            return_value="ledgers.bitcoin.MU_PER_SATOSHI is not set.",
        ):
            data = _report([], configured=("bitcoin",))

        self.assertEqual(
            _ledger(data, "bitcoin")["unavailable_reason"],
            "ledgers.bitcoin.MU_PER_SATOSHI is not set.",
        )


class TheReportCarriesTheRateTests(unittest.TestCase):
    """The rate is read from the ledger's own light module, JVM or no JVM."""

    def test_ergos_rate_names_its_key_and_both_scales(self):
        data = _report([_Contract()])
        rate = _ledger(data, "ergo")["rate"]

        self.assertEqual(rate["key"], "ledgers.ergo.payments.MU_PER_NANOERG")
        self.assertEqual(rate["symbol"], "ERG")
        # Per base unit is what the config key holds, so an edit changes what is shown.
        self.assertEqual(rate["mu_per_base_unit"], "1")
        # ...and per whole unit is what a peer is told, derived from it.
        self.assertEqual(rate["mu_per_unit"], "1000000000")
        self.assertEqual(rate["reason"], "")

    def test_an_unusable_rate_is_reported_as_a_reason_and_not_as_a_number(self):
        """The Bitcoin case the rate module exists for.

        An unset `MU_PER_SATOSHI` has no default and must not acquire one here: a
        borrowed rate misprices the node by a factor of a million.
        """
        data = _report([], configured=("bitcoin",))
        rate = _ledger(data, "bitcoin")["rate"]

        self.assertEqual(rate["key"], "ledgers.bitcoin.payments.MU_PER_SATOSHI")
        self.assertEqual(rate["mu_per_base_unit"], "")
        self.assertIn("MU_PER_SATOSHI", rate["reason"])


class TheReportSurvivesAChainThatWillNotAnswerTests(unittest.TestCase):
    """A read that fails costs one line, never the report."""

    def test_an_explorer_that_times_out_is_an_error_and_not_an_empty_history(self):
        # "Could not look" is not "nothing happened": an empty list would read as a
        # wallet nobody has ever used.
        data = _report([_Contract(raises=TimeoutError("explorer timed out"))])
        ergo = _ledger(data, "ergo")

        self.assertEqual(ergo["transactions"], [])
        self.assertIn("timed out", ergo["history_error"])
        # Everything that did not depend on the chain survives.
        self.assertEqual(ergo["address"], OURS)
        self.assertTrue(ergo["offered"])

    def test_a_wallet_that_cannot_be_read_costs_the_address_and_nothing_else(self):
        data = _report([_Contract(rows=[_row()], address=RuntimeError("no JVM"))])
        ergo = _ledger(data, "ergo")

        self.assertEqual(ergo["address"], "")
        self.assertEqual(len(ergo["transactions"]), 1)

    def test_a_contract_with_no_history_says_so_rather_than_reporting_none(self):
        class _Mute:
            LEDGER = "ergo"

            def get_wallet_address(self):
                return OURS

        data = _report([_Mute()])

        self.assertIn("does not report", _ledger(data, "ergo")["history_error"])


class TheRowsAreTheSameOnesThePageWouldPrintTests(unittest.TestCase):
    """Same normalisation, same counterparty join -- one computation, two renderings."""

    def test_a_row_carries_the_amount_in_its_own_money(self):
        # Never converted to MU: what a chain moved is denominated by that chain, and a
        # converted figure next to a confirmation count describes two different things.
        data = _report([_Contract(rows=[_row()])])
        row = _ledger(data, "ergo")["transactions"][0]

        self.assertEqual(row["amount"], "1.500000000 ERG")
        self.assertEqual(row["direction"], "in")
        self.assertEqual(row["confirmations"], 12)
        self.assertEqual(row["timestamp"], 1_700_000_000)

    def test_a_row_carries_whoever_this_node_can_name(self):
        data = _report([_Contract(rows=[_row()])])
        row = _ledger(data, "ergo")["transactions"][0]

        self.assertTrue(any(THEIRS in line for line in row["counterparty"]))

    def test_the_limit_is_the_one_asked_for(self):
        rows = [_row(tx_id=f"tx-{index}") for index in range(20)]

        self.assertEqual(len(_ledger(_report([_Contract(rows=rows)]), "ergo")["transactions"]),
                         tx_history.JSON_LIMIT)
        self.assertEqual(
            len(_ledger(_report([_Contract(rows=rows)], limit=3), "ergo")["transactions"]), 3
        )

    def test_the_json_limit_is_smaller_than_the_printed_pages(self):
        # The reader is a panel a few rows tall, and every extra row costs an explorer
        # page. A default that matched the printed command's would make the TUI's
        # periodic refresh twice the chain traffic it needs to be.
        self.assertLess(tx_history.JSON_LIMIT, 10)


if __name__ == "__main__":
    unittest.main()
