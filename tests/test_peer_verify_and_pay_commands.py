"""Unit tests for the `verify_reputation` and `pay_and_verify` dev commands.

These pin the command wiring — arg handling, reuse of the existing reputation /
payment primitives, and the safety guards — with everything on-chain / gRPC
mocked. No JVM, no network, no broadcast.
"""

import unittest
from unittest import mock

from src.commands import verify_reputation as vr
from src.commands import pay_and_verify as pv


class VerifyReputationCommandTests(unittest.TestCase):
    """`verify_reputation(peer_id)` reuses the proof_validation primitives."""

    def _patch(self, **overrides):
        # Sensible "happy path" defaults; individual tests override one piece.
        defaults = dict(
            _peer_reputation_proof_id=lambda peer_id: "proof-token-1",
            _get_unspent_boxes_by_token=lambda proof_id: [{"box": 1}],
            _boxes_off_canonical_contract=lambda boxes: [],
            _validate_box_structure=lambda box: True,
            _extract_register_value=lambda box, reg: "owner-r7-raw",
            _decode_coll_byte_hex=lambda value: "aabbccddeeff00112233",
            _challenge_peer_ownership=lambda peer_id, owner: True,
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

    def test_fail_when_peer_has_no_proof_id(self):
        self.assertFalse(self._run(_peer_reputation_proof_id=lambda peer_id: None))

    def test_fail_when_no_unspent_boxes(self):
        self.assertFalse(self._run(_get_unspent_boxes_by_token=lambda proof_id: []))

    def test_fail_when_box_off_canonical_contract(self):
        self.assertFalse(self._run(_boxes_off_canonical_contract=lambda boxes: ["deadbeef"]))

    def test_fail_when_box_structure_invalid(self):
        self.assertFalse(self._run(_validate_box_structure=lambda box: False))

    def test_fail_when_ownership_challenge_fails(self):
        # This is the crypto gate: structure is fine, but the peer can't sign.
        called = {}

        def challenge(peer_id, owner):
            called["args"] = (peer_id, owner)
            return False

        self.assertFalse(self._run(_challenge_peer_ownership=challenge))
        self.assertEqual(called["args"][0], "peer-1")


class PayAndVerifyCommandTests(unittest.TestCase):
    """`pay_and_verify(peer_id, amount_erg)` reuses the single-wallet payment flow."""

    def test_erg_to_gas_inverts_gas_to_nanoerg(self):
        with mock.patch.object(pv, "ConfigManager",
                               return_value=mock.Mock(get=lambda k: "1000")):
            # gas = erg_to_nanoerg(erg) * GAS_PER_ERG = (2 * 1e9) * 1000
            self.assertEqual(pv._erg_to_gas("2"), 2_000_000_000 * 1000)

    def test_rejects_invalid_amount(self):
        with mock.patch.object(pv, "ConfigManager",
                               return_value=mock.Mock(get=lambda k: "1000")):
            self.assertFalse(pv.pay_and_verify("peer-1", "not-a-number"))

    def _wire(self, balance_ok, scripts, paid):
        """Patch the deferred payment/ledger imports the command pulls at call time."""
        import src.payment_system.contracts.ergo.interface as ergo_iface
        import src.payment_system.payment_process as payment_process
        import src.database.access_functions.ledgers as ledgers

        return [
            mock.patch.object(pv, "ConfigManager",
                              return_value=mock.Mock(get=lambda k: "1000")),
            mock.patch.object(ergo_iface, "check_sender_balance",
                              return_value=balance_ok),
            mock.patch.object(ledgers, "get_peer_contract_instances",
                              return_value=iter(scripts)),
            mock.patch.object(payment_process, "increase_deposit_on_peer",
                              return_value=paid),
        ]

    def _run(self, balance_ok, scripts, paid):
        for p in self._wire(balance_ok, scripts, paid):
            p.start()
        self.addCleanup(mock.patch.stopall)
        return pv.pay_and_verify("peer-1", "1")

    def test_stops_cleanly_on_insufficient_balance(self):
        # No funded wallet -> stop before touching the payment flow.
        self.assertFalse(self._run(balance_ok=False, scripts=[(b"s", object())], paid=True))

    def test_stops_cleanly_when_peer_has_no_contract(self):
        self.assertFalse(self._run(balance_ok=True, scripts=[], paid=True))

    def test_pass_when_payment_completes_and_peer_verifies(self):
        self.assertTrue(self._run(balance_ok=True, scripts=[(b"s", object())], paid=True))

    def test_fail_when_payment_not_verified(self):
        self.assertFalse(self._run(balance_ok=True, scripts=[(b"s", object())], paid=False))


if __name__ == "__main__":
    unittest.main()
