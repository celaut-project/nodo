"""Unit tests for the `verify_reputation` and `pay` dev commands.

These pin the command wiring — arg handling, reuse of the existing reputation /
payment primitives, and the safety guards — with everything on-chain / gRPC
mocked. No JVM, no network, no broadcast.
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from protos import celaut_pb2
from src.commands import verify_reputation as vr
from src.commands import pay as pv


class VerifyReputationCommandTests(unittest.TestCase):
    """`verify_reputation(peer_id)` reuses the proof_validation primitives."""

    def _patch(self, **overrides):
        # Sensible "happy path" defaults; individual tests override one piece.
        defaults = dict(
            _peer_advertisement=lambda peer_id: celaut_pb2.Peer(),
            attested_wallet_public_key=lambda peer, ledger: "02" + "ab" * 32,
            _peer_reputation_proof_ids=lambda announced: ["proof-token-1"],
            _get_unspent_boxes_by_token=lambda proof_id: [{"box": 1}],
            _boxes_off_canonical_contract=lambda boxes: [],
            _validate_box_structure=lambda box: True,
            _extract_register_value=lambda box, reg: "owner-r7-raw",
            _decode_coll_byte_hex=lambda value: "aabbccddeeff00112233",
            node_proposition_hex=lambda wallet: "aabbccddeeff00112233",
        )
        defaults.update(overrides)
        return [mock.patch.object(vr, name, new=fn)
                for name, fn in defaults.items()]

    def _run(self, **overrides):
        patches = self._patch(**overrides)
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        return vr.verify_reputation("peer-1")

    def test_pass_when_all_checks_succeed(self):
        self.assertTrue(self._run())

    def test_fail_when_peer_announced_no_proof(self):
        self.assertFalse(self._run(_peer_reputation_proof_ids=lambda announced: []))

    def test_fail_when_the_peer_is_not_known(self):
        self.assertFalse(self._run(_peer_advertisement=lambda peer_id: None))

    def test_fail_when_no_ergo_wallet_is_attested(self):
        # R7 names a wallet, so a peer that cannot prove it holds one has proved
        # nothing about any proof on Ergo -- whatever the boxes themselves look like.
        self.assertFalse(
            self._run(attested_wallet_public_key=lambda peer, ledger: None)
        )

    def test_the_wallet_checked_is_the_attested_one(self):
        # Not the peer_id: the two are different keys now, and only one of them can
        # ever appear in R7.
        called = {}

        def node_proposition_hex(wallet):
            called["wallet"] = wallet
            return "aabbccddeeff00112233"

        self.assertTrue(self._run(
            attested_wallet_public_key=lambda peer, ledger: "02" + "cd" * 32,
            node_proposition_hex=node_proposition_hex,
        ))
        self.assertEqual(called["wallet"], "02" + "cd" * 32)

    def test_every_announced_proof_is_checked(self):
        # A peer can announce several proofs, and each is one of its published opinion
        # sets (issue #281). Verifying only the first would let a peer hide a proof it
        # does not own behind one it does.
        checked = []

        def boxes(proof_id):
            checked.append(proof_id)
            return [{"box": 1}]

        self.assertTrue(self._run(
            _peer_reputation_proof_ids=lambda announced: ["proof-a", "proof-b"],
            _get_unspent_boxes_by_token=boxes,
        ))
        self.assertEqual(checked, ["proof-a", "proof-b"])

    def test_one_unowned_proof_fails_the_peer(self):
        self.assertFalse(self._run(
            _peer_reputation_proof_ids=lambda announced: ["proof-a", "proof-b"],
            _get_unspent_boxes_by_token=lambda proof_id: [] if proof_id == "proof-b" else [{"box": 1}],
        ))

    def test_fail_when_no_unspent_boxes(self):
        self.assertFalse(self._run(_get_unspent_boxes_by_token=lambda proof_id: []))

    def test_fail_when_box_off_canonical_contract(self):
        self.assertFalse(self._run(_boxes_off_canonical_contract=lambda boxes: ["deadbeef"]))

    def test_fail_when_box_structure_invalid(self):
        self.assertFalse(self._run(_validate_box_structure=lambda box: False))

    def test_fail_when_the_attested_wallet_does_not_match_owner(self):
        # This is the crypto gate: structure is fine, the peer proved a wallet, and
        # that wallet is simply not the one that published the proof.
        self.assertFalse(
            self._run(node_proposition_hex=lambda wallet: "some-other-owner")
        )


class PayCommandTests(unittest.TestCase):
    """`pay(peer_id, amount_erg)` reuses the single-wallet payment flow and, on
    success, reads back this node's balance registered on the peer."""

    def test_erg_to_mu_uses_the_peg(self):
        # 1 MU == 1 nanoERG, so 2 ERG is exactly 2e9 MU. No configured factor is
        # involved any more -- the old GAS_PER_ERG put a payment 49 orders of
        # magnitude away from any charge the node computed.
        self.assertEqual(pv._erg_to_mu("2"), 2_000_000_000)

    def test_rejects_invalid_amount(self):
        self.assertFalse(pv.pay("peer-1", "not-a-number"))

    def _wire(self, balance_ok, scripts, paid, readback=None):
        """Patch the deferred payment/ledger imports the command pulls at call time.

        ``readback`` optionally patches ``pay._read_peer_balance`` so the post-payment
        readback is deterministic without a real DB. It is a list consumed as
        successive return values (before, after)."""
        import src.payment_system.contracts.ergo.interface as ergo_iface
        import src.payment_system.payment_process as payment_process
        import src.database.access_functions.ledgers as ledgers

        patches = [
            mock.patch.object(ergo_iface, "check_sender_balance",
                              return_value=balance_ok),
            mock.patch.object(ledgers, "get_peer_contract_instances",
                              return_value=iter(scripts)),
            mock.patch.object(payment_process, "increase_deposit_on_peer",
                              return_value=paid),
        ]
        if readback is not None:
            patches.append(
                mock.patch.object(pv, "_read_peer_balance", side_effect=list(readback))
            )
        return patches

    def _run(self, balance_ok, scripts, paid, readback=None, capture=False):
        for p in self._wire(balance_ok, scripts, paid, readback):
            p.start()
        self.addCleanup(mock.patch.stopall)
        buf = io.StringIO()
        if capture:
            with redirect_stdout(buf):
                result = pv.pay("peer-1", "1")
            return result, buf.getvalue()
        return pv.pay("peer-1", "1")

    def test_stops_cleanly_on_insufficient_balance(self):
        # No funded wallet -> stop before touching the payment flow.
        self.assertFalse(self._run(balance_ok=False, scripts=[(b"s", object())], paid=True))

    def test_stops_cleanly_when_peer_has_no_contract(self):
        self.assertFalse(self._run(balance_ok=True, scripts=[], paid=True))

    def test_pass_when_payment_completes(self):
        # balance readback stubbed: (before, after) -> it grew by the paid amount.
        before = (5_000, 1000, "2026-07-31T09:00:00")
        after = (7_000, 1000, "2026-07-31T10:00:00")
        self.assertTrue(
            self._run(balance_ok=True, scripts=[(b"s", object())], paid=True,
                      readback=[before, after])
        )

    def test_fail_when_payment_not_accepted(self):
        self.assertFalse(self._run(balance_ok=True, scripts=[(b"s", object())], paid=False))

    def test_prints_balance_readback_on_successful_pay(self):
        # The whole point of the command post-rework: after a successful pay it
        # reads back and prints THIS node's balance registered on the peer.
        before = (5_000, 1000, "2026-07-31T09:00:00")
        after = (7_000, 1000, "2026-07-31T10:00:00")
        result, out = self._run(
            balance_ok=True, scripts=[(b"s", object())], paid=True,
            readback=[before, after], capture=True,
        )
        self.assertTrue(result)
        self.assertIn("now credits you", out)
        self.assertIn("since before this payment", out)
        # And it must NOT claim a local (payer-side) verify happened.
        self.assertNotIn("VERIFIED", out)

    def test_prints_transaction_url_when_payment_reports_it(self):
        before = (5_000, 1000, "2026-07-31T09:00:00")
        after = (7_000, 1000, "2026-07-31T10:00:00")
        transaction_url = "https://sigmaspace.io/en/transaction/abc123"

        patches = self._wire(True, [(b"s", object())], True, [before, after])
        for patch in patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)

        import src.payment_system.payment_process as payment_process
        payment_process.increase_deposit_on_peer.side_effect = (
            lambda **kwargs: kwargs["on_transaction_url"](transaction_url) or True
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(pv.pay("peer-1", "1"))

        self.assertIn(f"Transaction URL: {transaction_url}", buf.getvalue())

    def test_paying_line_is_in_erg_and_shows_no_internal_unit(self):
        """The operator asked to pay in ERG and should read ERG back.

        This replaces a regression test that asserted the line sci-formatted a ~1e63
        gas integer. That number existed only because the unit was undefined; with the
        peg there is nothing monstrous left to format, and MU must not surface here.
        """
        before = (5_000, 1000, "2026-07-31T09:00:00")
        after = (7_000, 1000, "2026-07-31T10:00:00")
        _result, out = self._run(
            balance_ok=True, scripts=[(b"s", object())], paid=True,
            readback=[before, after], capture=True,
        )
        self.assertIn("Paying 1 ERG to peer peer-1", out)
        self.assertNotIn("MU", out)
        self.assertNotIn("gas", out)
        # The readback is ERG too: 7000 MU is 0.000007 ERG.
        self.assertIn("0.000007 ERG", out)


if __name__ == "__main__":
    unittest.main()
