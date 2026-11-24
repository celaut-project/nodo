"""The pointers to the proofs on ledgers in a ReputationProof (``ledger_proofs``).

They were ``Peer.reputation_proofs``. They are in the proof that the publisher signs with
its identity key, so a node that passes the proof on cannot add or remove one.
"""
import unittest
from unittest.mock import patch

from tests.reputation_system.test_offchain_reputation import (
    IMPORT_ERROR, NOW, _Db, _Signer, _score,
)

if IMPORT_ERROR is None:
    from protos import celaut_pb2
    from src.reputation_system import offchain_proof
    from src.utils.contract_xattrs import get_token_id, set_token_id


def _pointer(token_id):
    contract = celaut_pb2.Contract()
    contract.ledger.tags.append("ergo")
    set_token_id(contract, token_id)
    return contract


def _signed(signer, token_ids, ts=NOW):
    proof = celaut_pb2.ReputationProof(ts=ts)
    proof.opinions.append(_score("ab" * 32, 40))
    proof.ledger_proofs.extend(_pointer(token_id) for token_id in token_ids)
    with signer:
        return offchain_proof.sign(proof)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerProofsSignatureTests(unittest.TestCase):

    def setUp(self):
        self.publisher = _Signer()

    def test_a_signed_proof_with_pointers_verifies(self):
        self.assertTrue(offchain_proof.verify(_signed(self.publisher, ["aa", "bb"])))

    def test_an_added_pointer_does_not_verify(self):
        proof = _signed(self.publisher, ["aa"])
        proof.ledger_proofs.append(_pointer("grafted"))
        self.assertFalse(offchain_proof.verify(proof))

    def test_a_removed_pointer_does_not_verify(self):
        proof = _signed(self.publisher, ["aa", "bb"])
        del proof.ledger_proofs[1]
        self.assertFalse(offchain_proof.verify(proof))

    def test_a_changed_pointer_does_not_verify(self):
        proof = _signed(self.publisher, ["aa"])
        set_token_id(proof.ledger_proofs[0], "cc")
        self.assertFalse(offchain_proof.verify(proof))

    def test_the_order_of_the_pointers_does_not_change_the_digest(self):
        first = _signed(self.publisher, ["aa", "bb"])
        second = celaut_pb2.ReputationProof()
        second.CopyFrom(first)
        del second.ledger_proofs[:]
        second.ledger_proofs.extend([first.ledger_proofs[1], first.ledger_proofs[0]])
        self.assertEqual(offchain_proof.proof_digest(first), offchain_proof.proof_digest(second))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerProofsReceiveTests(_Db, unittest.TestCase):

    def setUp(self):
        _Db.setUp(self)
        self.me = _Signer()
        self.me.__enter__()
        self.addCleanup(self.me.__exit__)
        self.publisher = _Signer()

    def _signed_by_publisher(self, token_ids):
        self.me.__exit__()
        try:
            return _signed(self.publisher, token_ids)
        finally:
            self.me.__enter__()

    def test_a_proof_with_too_many_pointers_is_refused(self):
        self.add_peer(self.publisher.peer_id)
        proof = self._signed_by_publisher([f"{n:02x}" for n in range(offchain_proof.MAX_LEDGER_PROOFS + 1)])
        self.assertEqual(offchain_proof.receive(proof.SerializeToString()),
                         "too many ledger proofs")
        self.assertEqual(self.sql.get_offchain_proofs(), [])

    def test_the_pointers_of_a_kept_proof_are_read_back(self):
        self.add_peer(self.publisher.peer_id)
        proof = self._signed_by_publisher(["aa", "bb"])
        with patch.object(offchain_proof, "check_ledger_proofs"):
            self.assertIsNone(offchain_proof.receive(proof.SerializeToString()))
        kept = offchain_proof.stored_ledger_proofs(self.publisher.peer_id)
        self.assertEqual([get_token_id(c) for c in kept], ["aa", "bb"])

    def test_each_pointer_of_a_kept_proof_is_checked_against_its_publisher(self):
        self.add_peer(self.publisher.peer_id)
        proof = self._signed_by_publisher(["aa", "bb"])
        with patch("src.manager.manager.validate_reputation_proof") as validate:
            self.assertIsNone(offchain_proof.receive(proof.SerializeToString()))
        self.assertEqual(
            [(get_token_id(call.kwargs["contract_ledger"]), call.kwargs["peer_id"])
             for call in validate.call_args_list],
            [("aa", self.publisher.peer_id), ("bb", self.publisher.peer_id)],
        )

    def test_a_peer_with_no_kept_proof_has_no_pointers(self):
        self.assertEqual(offchain_proof.stored_ledger_proofs(self.publisher.peer_id), [])

    def test_unreadable_bytes_give_no_pointers(self):
        self.assertEqual(offchain_proof.ledger_proofs_of(b"\xff\xff not a proof"), [])
        self.assertEqual(offchain_proof.ledger_proofs_of(None), [])


if __name__ == "__main__":
    unittest.main()
