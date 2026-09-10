"""Unit tests for the `verify_reputation` and `pay` dev commands.

These pin the command wiring — arg handling, reuse of the existing reputation /
payment primitives, and the safety guards — with everything on-chain / gRPC
mocked. No JVM, no network, no broadcast.
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from hashlib import sha3_256

from protos import celaut_pb2
from src.commands import verify_reputation as vr
from src.payment_system.mu_conversion import MatchingPaymentSystem
from src.utils.contract_xattrs import get_token_id, set_token_id


from src.commands import pay as pv


def _announcement(proof_ids):
    """A stored advertisement carrying the given proofs."""
    peer = celaut_pb2.Peer()
    for proof_id in proof_ids:
        set_token_id(peer.reputation_proofs.add(), proof_id)
    return peer


class VerifyReputationCommandTests(unittest.TestCase):
    """`verify_reputation(peer_id)` runs the node's own check, not a copy of it.

    The command's job is reading the advertisement, resolving each proof's attested
    owner, and reporting; the verdict itself comes from `explain_contract_ledger`.
    These mock that boundary rather than the primitives beneath it -- mocking those
    was what let the two implementations drift apart unnoticed.
    """

    def _patch(self, **overrides):
        # Sensible "happy path" defaults; individual tests override one piece.
        defaults = dict(
            _peer_advertisement=lambda peer_id: _announcement(["proof-token-1"]),
            attested_proof_owner=lambda contract, peer_id: "02" + "ab" * 32,
            explain_contract_ledger=lambda contract, wallet: None,
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
        self.assertFalse(self._run(_peer_advertisement=lambda peer_id: celaut_pb2.Peer()))

    def test_fail_when_the_peer_is_not_known(self):
        self.assertFalse(self._run(_peer_advertisement=lambda peer_id: None))

    def test_fail_when_no_owner_is_attested(self):
        # R7 names a wallet, so a proof whose owner this peer cannot prove has proved
        # nothing -- whatever the boxes themselves look like.
        self.assertFalse(
            self._run(attested_proof_owner=lambda contract, peer_id: None)
        )

    def test_the_node_verdict_is_what_decides(self):
        # Not a re-derivation of it: whatever the shared check says, the command says.
        self.assertFalse(
            self._run(explain_contract_ledger=lambda contract, wallet: "off-contract")
        )

    def test_the_reason_reaches_the_operator(self):
        # The whole point of the command over the node's log: which of the checks
        # failed, in words, rather than a bare verdict.
        out = io.StringIO()
        with redirect_stdout(out):
            self._run(
                explain_contract_ledger=lambda contract, wallet: (
                    "Contract ledger not compatible: ledger=False script=True"
                )
            )
        self.assertIn("Contract ledger not compatible", out.getvalue())

    def test_the_wallet_checked_is_the_attested_one(self):
        # Not the peer_id: the two are different keys, and only the wallet can ever
        # appear in R7.
        called = {}

        def explain(contract, wallet):
            called["wallet"] = wallet
            return None

        self.assertTrue(self._run(
            attested_proof_owner=lambda contract, peer_id: "02" + "cd" * 32,
            explain_contract_ledger=explain,
        ))
        self.assertEqual(called["wallet"], "02" + "cd" * 32)

    def test_every_announced_proof_is_checked(self):
        # A peer can announce several proofs, and each is one of its published opinion
        # sets (issue #281). Verifying only the first would let a peer hide a proof it
        # does not own behind one it does.
        checked = []

        def explain(contract, wallet):
            checked.append(get_token_id(contract))
            return None

        self.assertTrue(self._run(
            _peer_advertisement=lambda peer_id: _announcement(["proof-a", "proof-b"]),
            explain_contract_ledger=explain,
        ))
        self.assertEqual(checked, ["proof-a", "proof-b"])

    def test_one_unowned_proof_fails_the_peer(self):
        self.assertFalse(self._run(
            _peer_advertisement=lambda peer_id: _announcement(["proof-a", "proof-b"]),
            explain_contract_ledger=lambda contract, wallet: (
                "no unspent boxes" if get_token_id(contract) == "proof-b" else None
            ),
        ))


class OneCheckTwoAudiencesTests(unittest.TestCase):
    """The node and the command must never be able to answer differently.

    They did: `verify_reputation` walked the same six steps in its own words and had
    lost the first two, so it printed PASS for a proof on a non-canonical ledger --
    the one the node refuses with "Not supported reputation contract", and exactly the
    case an operator runs the command to understand.
    """

    def test_the_command_and_the_node_call_the_same_function(self):
        from src.reputation_system.contracts.ergo import proof_validation

        self.assertIs(vr.explain_contract_ledger, proof_validation.explain_contract_ledger)

    def test_the_verdict_is_the_reason_being_absent(self):
        # `validate_contract_ledger` is a thin wrapper, so a caller wanting a bool and
        # a caller wanting words cannot end up with different answers.
        from src.reputation_system.contracts.ergo import proof_validation

        with mock.patch.object(
            proof_validation, "explain_contract_ledger", lambda c, w: None
        ):
            self.assertTrue(proof_validation.validate_contract_ledger(object(), "w"))
        with mock.patch.object(
            proof_validation, "explain_contract_ledger", lambda c, w: "nope"
        ):
            self.assertFalse(proof_validation.validate_contract_ledger(object(), "w"))

    def test_a_non_canonical_ledger_is_refused(self):
        # The check the command had lost. A Contract that is not the canonical
        # reputation contract never reaches the chain lookups.
        from protos import celaut_pb2
        from src.reputation_system.contracts.ergo import proof_validation

        foreign = celaut_pb2.Contract()
        foreign.ledger.formal = b"some other ledger"
        reason = proof_validation.explain_contract_ledger(foreign, "02" + "ab" * 32)

        self.assertIsNotNone(reason)
        self.assertIn("not compatible", reason)


# The peer shares Ergo with us, at the shipped rate on both sides.
PAY_SYSTEM = MatchingPaymentSystem(
    ledger_tag="ergo",
    contract_hash=sha3_256("proveDlog(decodePoint())".encode("utf-8")).hexdigest(),
    local_mu_per_unit=1_000_000_000,
    peer_mu_per_unit=1_000_000_000,
)


class PayCommandTests(unittest.TestCase):
    """`pay(peer_id, amount_erg)` reuses the single-wallet payment flow and, on
    success, reads back this node's balance registered on the peer."""

    def test_the_amount_is_converted_through_the_contracts_own_rate(self):
        """1 MU == 1 nanoERG by default, so 2 ERG is exactly 2e9 MU.

        Converted through `mu_per_unit()` -- the same figure peers are told as
        `ContractRate` -- so what the operator types and what the peer credits are
        related by one number rather than by two conversions that can disagree.
        """
        from src.payment_system.contracts.ergo import interface as ergo

        self.assertEqual(pv._amount_to_mu(ergo, "2"), 2_000_000_000)

    def test_each_ledger_reads_the_amount_in_its_own_unit(self):
        # The same typed string means different money on different chains, which is
        # exactly why --ledger exists once a node offers more than one.
        contract = mock.Mock()
        contract.mu_per_unit.return_value = 200_000_000_000_000  # MU per whole BTC
        contract.NATIVE_ASSET = "BTC"
        self.assertEqual(pv._amount_to_mu(contract, "0.001"), 200_000_000_000)

    def test_an_amount_that_is_not_a_whole_number_of_mu_is_refused(self):
        # MU is the unit of account; there is nothing smaller to express, so a figure
        # that would need a fraction of one is refused rather than rounded.
        contract = mock.Mock()
        contract.mu_per_unit.return_value = 10
        contract.NATIVE_ASSET = "BTC"
        with self.assertRaisesRegex(ValueError, "whole number of MU"):
            pv._amount_to_mu(contract, "0.05")

    def test_several_payment_systems_without_a_ledger_is_refused(self):
        """Two systems are two currencies, so the amount is ambiguous.

        Guessing would move money on a chain nobody named.
        """
        first, second = mock.Mock(), mock.Mock()
        first.LEDGER, second.LEDGER = "ergo", "bitcoin"
        first.is_demo = second.is_demo = False
        with mock.patch("src.payment_system.contracts.registry.contracts",
                        return_value={"a": first, "b": second}):
            contract, refusal = pv._contract_for(None)
        self.assertIsNone(contract)
        self.assertIn("--ledger", refusal)

    def test_a_named_ledger_the_node_does_not_offer_is_refused_by_name(self):
        only = mock.Mock()
        only.LEDGER, only.is_demo = "ergo", False
        with mock.patch("src.payment_system.contracts.registry.contracts",
                        return_value={"a": only}):
            contract, refusal = pv._contract_for("bitcoin")
            self.assertIsNone(contract)
            self.assertIn("ergo", refusal)
            # And the one it does offer needs no flag at all.
            self.assertIs(pv._contract_for(None)[0], only)

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

        # A fresh iterator per call, not one shared: the command asks for the peer's
        # instances, and so does the advertisement read behind `deposit_refusal_reason`.
        # A single `iter(...)` is exhausted by whoever gets there first.
        rows = list(scripts)
        patches = [
            mock.patch.object(ergo_iface, "check_sender_balance",
                              return_value=balance_ok),
            mock.patch.object(ledgers, "get_peer_contract_instances",
                              side_effect=lambda *a, **k: iter(list(rows))),
            mock.patch.object(payment_process, "increase_deposit_on_peer",
                              return_value=paid),
            # The peer shares Ergo with us, at the same rate. Patched rather than left
            # to the catalogue: this is a test about the command, and resolving a real
            # payment system would need advertised rows on both sides.
            mock.patch("src.payment_system.mu_conversion.matching_payment_systems",
                       return_value=[PAY_SYSTEM]),
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
