"""The proof of work of an off-chain reputation proof (#677)."""
import time
import unittest
from unittest.mock import patch

from tests.reputation_system.test_offchain_reputation import (
    IMPORT_ERROR, NOW, _Db, _Signer, _score,
)

if IMPORT_ERROR is None:
    from protos import celaut_pb2
    from src.identity import execution_receipt
    from src.reputation_system import offchain_indexer, offchain_proof

SUBJECT = "ab" * 32


def _settings(**values):
    return patch.object(offchain_proof, "_setting",
                        side_effect=lambda key, default: values.get(key, default))


def _mined(signer, opinions, bits, ts=NOW):
    """A proof of ``signer`` with at least ``bits`` of work, signed."""
    proof = celaut_pb2.ReputationProof(ts=ts)
    proof.opinions.extend(opinions)
    with signer:
        proof.publisher = signer.peer_id
        proof.signature_scheme.CopyFrom(execution_receipt.receipt_scheme())
        offchain_proof.mine(proof, bits)
        return offchain_proof.sign(proof)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WorkTests(unittest.TestCase):

    def setUp(self):
        self.signer = _Signer()

    def test_zero_bits_counts_the_leading_zero_bits(self):
        self.assertEqual(offchain_proof.zero_bits(b"\x00\x00"), 16)
        self.assertEqual(offchain_proof.zero_bits(b"\x00\x10"), 11)
        self.assertEqual(offchain_proof.zero_bits(b"\x80\x00"), 0)

    def test_the_work_of_a_proof_is_the_zero_bits_of_its_digest(self):
        proof = _mined(self.signer, [_score(SUBJECT, 40)], bits=10)
        self.assertGreaterEqual(offchain_proof.pow_bits(proof), 10)
        self.assertEqual(offchain_proof.pow_bits(proof),
                         offchain_proof.zero_bits(bytes.fromhex(offchain_proof.proof_digest(proof))))

    def test_a_mined_proof_verifies(self):
        self.assertTrue(offchain_proof.verify(_mined(self.signer, [_score(SUBJECT, 40)], 8)))

    def test_the_signature_covers_the_nonce(self):
        proof = _mined(self.signer, [_score(SUBJECT, 40)], 8)
        proof.pow_nonce += 1
        self.assertFalse(offchain_proof.verify(proof))

    def test_the_work_depends_on_what_the_proof_says(self):
        one = _mined(self.signer, [_score(SUBJECT, 40)], 8)
        other = celaut_pb2.ReputationProof()
        other.CopyFrom(one)
        other.opinions[0].amount = 41
        self.assertNotEqual(offchain_proof.proof_digest(one), offchain_proof.proof_digest(other))

    def test_mine_stops_when_it_is_told_to(self):
        import threading
        stop = threading.Event()
        stop.set()
        proof = celaut_pb2.ReputationProof(ts=NOW, publisher=self.signer.peer_id)
        self.assertFalse(offchain_proof.mine(proof, 40, stop=stop))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FactorTests(unittest.TestCase):

    def test_with_no_target_the_work_changes_nothing(self):
        with _settings():
            self.assertEqual(offchain_proof.pow_factor(0), 1.0)

    def test_the_whole_voice_is_at_the_target_and_above(self):
        with _settings(TARGET_OFFCHAIN_POW_BITS=10):
            self.assertEqual(offchain_proof.pow_factor(10), 1.0)
            self.assertEqual(offchain_proof.pow_factor(14), 1.0)

    def test_the_factor_is_linear_in_the_work_not_in_the_bits(self):
        with _settings(TARGET_OFFCHAIN_POW_BITS=10):
            self.assertEqual(offchain_proof.pow_factor(9), 0.5)
            self.assertEqual(offchain_proof.pow_factor(8), 0.25)

    def test_splitting_the_work_between_identities_gives_no_more_voice(self):
        with _settings(TARGET_OFFCHAIN_POW_BITS=10):
            # 4 identities with 8 bits each did 4 * 2^8 tries; one with 10 bits did 2^10.
            self.assertAlmostEqual(4 * offchain_proof.pow_factor(8),
                                   offchain_proof.pow_factor(10))

    def test_the_settings_are_bounded(self):
        with _settings(TARGET_OFFCHAIN_POW_BITS=500, MIN_OFFCHAIN_POW_BITS=-3):
            self.assertEqual(offchain_proof.target_pow_bits(), offchain_proof.MAX_POW_BITS)
            self.assertEqual(offchain_proof.min_pow_bits(), 0)
        with _settings(TARGET_OFFCHAIN_POW_BITS="many"):
            self.assertEqual(offchain_proof.target_pow_bits(), 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReceiveAndScoreTests(_Db, unittest.TestCase):

    def setUp(self):
        _Db.setUp(self)
        self.me = _Signer()
        self.me.__enter__()
        self.addCleanup(self.me.__exit__)
        self.publisher = _Signer()
        self.add_peer(self.publisher.peer_id)

    def _make(self, bits, opinions=None, ts=NOW):
        self.me.__exit__()
        try:
            return _mined(self.publisher, opinions or [_score(SUBJECT, 40)], bits, ts)
        finally:
            self.me.__enter__()

    def test_a_proof_under_the_minimum_is_refused(self):
        # A proof with no work has the bits of one hash: take one that has fewer than 8.
        proof = next(p for p in (self._make(bits=0, ts=NOW - i) for i in range(50))
                     if offchain_proof.pow_bits(p) < 8)
        with _settings(MIN_OFFCHAIN_POW_BITS=8):
            self.assertEqual(offchain_proof.receive(proof.SerializeToString()),
                             "not enough work")
        self.assertEqual(self.sql.get_offchain_proofs(), [])

    def test_a_proof_with_enough_work_is_kept(self):
        proof = self._make(bits=8)
        with _settings(MIN_OFFCHAIN_POW_BITS=8):
            self.assertIsNone(offchain_proof.receive(proof.SerializeToString()))
        self.assertEqual(len(self.sql.get_offchain_proofs()), 1)

    def test_the_voice_is_scaled_by_the_work(self):
        proof = self._make(bits=8)
        local = {self.publisher.peer_id: 0.5, SUBJECT: 0.5}
        with _settings():
            plain = offchain_indexer.opinion_rows([proof], local, 50.0)
        with _settings(TARGET_OFFCHAIN_POW_BITS=offchain_proof.pow_bits(proof) + 1):
            half = offchain_indexer.opinion_rows([proof], local, 50.0)
        self.assertEqual(len(plain), 1)
        self.assertAlmostEqual(half[0][3], plain[0][3] / 2)

    def test_refresh_drops_a_kept_proof_when_the_minimum_rises(self):
        proof = self._make(bits=8)
        self.sql.store_offchain_proof(self.publisher.peer_id, proof.ts,
                                      proof.SerializeToString())
        bits = offchain_proof.pow_bits(proof)
        with _settings(MIN_OFFCHAIN_POW_BITS=min(bits + 1, 32)), \
                patch("src.reputation_system.onchain_indexer.local_scores", return_value={}):
            offchain_indexer.refresh()
        self.assertEqual(self.sql.get_offchain_proofs(), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OwnProofTests(_Db, unittest.TestCase):

    def setUp(self):
        _Db.setUp(self)
        self.me = _Signer()
        self.me.__enter__()
        self.addCleanup(self.me.__exit__)
        self.addCleanup(offchain_proof.forget_own_proof)
        offchain_proof.forget_own_proof()

    def _wait_for(self, bits, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            proof = offchain_proof.own_proof()
            if proof is not None and offchain_proof.pow_bits(proof) >= bits:
                return proof
            time.sleep(0.05)
        self.fail("the proof was not mined in time")

    def test_with_no_target_the_proof_has_no_work_to_do(self):
        with _settings():
            self.assertEqual(offchain_proof.own_pow_bits(), 0)
            self.assertIsNotNone(offchain_proof.own_proof())

    def test_the_own_proof_is_mined_in_a_thread_and_verifies(self):
        with _settings(TARGET_OFFCHAIN_POW_BITS=10):
            self.assertEqual(offchain_proof.own_pow_bits(), 10)
            proof = self._wait_for(10)
            self.assertTrue(offchain_proof.verify(proof))

    def test_the_minimum_is_a_floor_for_the_own_work(self):
        with _settings(TARGET_OFFCHAIN_POW_BITS=4, MIN_OFFCHAIN_POW_BITS=9):
            self.assertEqual(offchain_proof.own_pow_bits(), 9)

    def test_a_proof_that_says_the_same_is_not_mined_again(self):
        with _settings(TARGET_OFFCHAIN_POW_BITS=8):
            first = self._wait_for(8)
            with patch.object(offchain_proof, "OWN_PROOF_SECONDS", 0), \
                    patch.object(offchain_proof, "mine") as mined:
                again = offchain_proof.own_proof()
            mined.assert_not_called()
        self.assertEqual((again.ts, again.pow_nonce, again.signature),
                         (first.ts, first.pow_nonce, first.signature))
