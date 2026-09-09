"""Reading donations off Ergo, and deciding whose they were.

The signal never travels over the protocol -- a peer's own claim about its generosity
would be forgeable -- so every node reads the chain itself, and every rule here exists
to stop it crediting the wrong node:

* Only outputs paying an address this node *counts* are donations at all.
* The donor is the transaction's input address, and only when every input shares one.
  A mixed-input transaction has no single donor and is skipped rather than guessed at.
* Nothing below the confirmation threshold counts, and the mempool is never read.

The link from an address to a peer is economic, not cryptographic: a peer announces
the address it wants to be *paid* at, so announcing one it does not control means
giving its revenue away, and claiming another peer's address to steal that peer's
donation credit costs 100 % of its income in that currency.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import donation_scan
    from src.utils.ergo_units import p2pk_public_key_from_address
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    donation_scan = None  # type: ignore[assignment]

COUNTED = "9gGZp7HRAFxgGWSwvS4hCbxM2RpkYr6pHvwpU4GPrpvxY7Y2nQo"
DONOR = "9hHDQb26AjnJUXxcqriqY1mnhpLuUeC81C4pggtK7tupr92Ea1K"
OTHER = "9hXmgvzndtakdSAgJ92fQ8ZjuKirWAw8tyDuyJrXP6sKHVpCz8a"


def _tx(tx_id="tx-1", inputs=(DONOR,), outputs=((COUNTED, 5_000_000),),
        height=100, confirmations=20):
    return {
        "id": tx_id,
        "inclusionHeight": height,
        "numConfirmations": confirmations,
        "inputs": [{"address": address} for address in inputs],
        "outputs": [{"address": address, "value": value} for address, value in outputs],
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ScanTests(unittest.TestCase):

    def _scan(self, transactions, *, from_height=0, min_confirmations=10):
        pages = [{"items": transactions}, {"items": []}]
        with mock.patch.object(donation_scan, "_get", side_effect=pages):
            return donation_scan.scan_address(
                COUNTED, from_height=from_height, min_confirmations=min_confirmations
            )

    def test_a_single_input_transaction_is_attributed_to_its_input_address(self):
        found, truncated = self._scan([_tx()])
        self.assertFalse(truncated)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].from_address, DONOR)
        self.assertEqual(found[0].to_address, COUNTED)
        self.assertEqual(found[0].amount_native, 5_000_000)
        self.assertEqual(found[0].token_id, "ERG")

    def test_a_mixed_input_transaction_is_skipped_rather_than_guessed_at(self):
        found, _ = self._scan([_tx(inputs=(DONOR, OTHER))])
        self.assertEqual(found, [])

    def test_only_the_outputs_paying_us_are_counted(self):
        # The change going back to the donor, and anything paying a third party, are
        # not donations to this node.
        found, _ = self._scan([_tx(outputs=((COUNTED, 5_000_000), (DONOR, 90_000_000),
                                            (OTHER, 1_000_000)))])
        self.assertEqual(found[0].amount_native, 5_000_000)

    def test_several_outputs_to_us_in_one_transaction_are_summed(self):
        found, _ = self._scan([_tx(outputs=((COUNTED, 2_000_000), (COUNTED, 3_000_000)))])
        self.assertEqual(found[0].amount_native, 5_000_000)

    def test_a_transaction_paying_nobody_we_count_is_not_a_donation(self):
        found, _ = self._scan([_tx(outputs=((OTHER, 5_000_000),))])
        self.assertEqual(found, [])

    def test_a_transaction_below_the_confirmation_threshold_does_not_count(self):
        found, _ = self._scan([_tx(confirmations=3)], min_confirmations=10)
        self.assertEqual(found, [])

    def test_the_scan_stops_at_the_cursor(self):
        # The explorer returns newest first, so a chain already indexed costs one page.
        found, _ = self._scan([_tx(tx_id="new", height=200), _tx(tx_id="old", height=50)],
                              from_height=100)
        self.assertEqual([donation.tx_id for donation in found], ["new"])

    def test_an_address_paying_itself_is_not_a_donation(self):
        found, _ = self._scan([_tx(inputs=(COUNTED,))])
        self.assertEqual(found, [])

    def test_an_unreachable_explorer_is_undetermined_and_not_an_empty_answer(self):
        # Cached rows have to stand: read as "no donations", an explorer outage would
        # silently zero every peer's credit.
        with mock.patch.object(
            donation_scan, "_get", side_effect=donation_scan.ExplorerUnavailable("down")
        ):
            with self.assertRaises(donation_scan.ExplorerUnavailable):
                donation_scan.scan_address(COUNTED, from_height=0, min_confirmations=10)

    def test_a_truncated_scan_says_so(self):
        # A page cap that read as "fully indexed" would let the cursor jump past
        # transactions nobody read.
        page = {"items": [_tx(tx_id=f"tx-{i}", height=1000 - i)
                          for i in range(donation_scan._PAGE_SIZE)]}
        with mock.patch.object(donation_scan, "_get", return_value=page):
            _, truncated = donation_scan.scan_address(
                COUNTED, from_height=0, min_confirmations=10
            )
        self.assertTrue(truncated)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DonorIdentityTests(unittest.TestCase):

    def test_a_donor_address_derives_the_bytes_a_peer_announced(self):
        """The join between a chain and the peer table, done purely.

        A payment contract instance is stored as the raw P2PK propositionBytes in hex
        (`add_contract`), which is what the peer announced. Deriving the same bytes from
        the address the explorer reports is what identifies the donor -- and it must not
        need a JVM, because indexing is a read.
        """
        values = list(donation_scan.instance_values_for(DONOR))
        key = p2pk_public_key_from_address(DONOR)
        self.assertEqual(values, ["0008cd" + key.hex()])

    def test_an_address_that_is_not_p2pk_identifies_nobody(self):
        # A script address encodes a whole script, not one public key, so there is
        # nothing to match a peer's announcement against.
        self.assertEqual(list(donation_scan.instance_values_for("not-an-address")), [])

    def test_ergos_block_time_is_stated_in_seconds(self):
        # Age is measured in seconds so the same old donation is not weighed five times
        # differently on a chain with a five-times-longer block.
        self.assertEqual(donation_scan.SECONDS_PER_BLOCK, 120)


if __name__ == "__main__":
    unittest.main()
