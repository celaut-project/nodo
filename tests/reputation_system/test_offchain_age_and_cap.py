"""The cap of the voices on one subject, and the maximum age of a kept proof (#659)."""
import time
import unittest
from unittest.mock import patch

from tests.reputation_system.test_offchain_reputation import (
    IMPORT_ERROR, NOW, _Db, _Signer, _proof, _score,
)

if IMPORT_ERROR is None:
    from src.reputation_system import offchain_credit, offchain_indexer, offchain_proof

DAY = 86400


def _row(subject, publisher, verdict, weight):
    return {"subject_id": subject, "publisher": publisher, "verdict": verdict,
            "weight": weight}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SubjectVoiceCapTests(unittest.TestCase):

    def test_with_no_cap_many_identities_make_a_strong_voice(self):
        rows = [_row("s", f"p{i}", 0.9, 0.5) for i in range(100)]
        self.assertGreater(offchain_credit.standing(rows, 1.0)["s"], 0.85)

    def test_the_cap_bounds_the_voices_of_many_identities(self):
        rows = [_row("s", f"p{i}", 0.9, 0.5) for i in range(100)]
        capped = offchain_credit.standing(rows, 1.0, voice_cap=5.0)["s"]
        self.assertAlmostEqual(capped, 0.9 * 5.0 / 6.0)

    def test_the_cap_keeps_the_weighted_mean(self):
        rows = ([_row("s", f"a{i}", 1.0, 1.0) for i in range(6)]
                + [_row("s", f"b{i}", -1.0, 1.0) for i in range(6)])
        self.assertAlmostEqual(offchain_credit.standing(rows, 1.0, voice_cap=5.0)["s"], 0.0)

    def test_a_subject_under_the_cap_is_not_changed(self):
        rows = [_row("s", "p1", 0.8, 0.5), _row("s", "p2", 0.4, 1.0)]
        self.assertEqual(offchain_credit.standing(rows, 1.0, voice_cap=5.0),
                         offchain_credit.standing(rows, 1.0))

    def test_the_cap_is_for_each_subject(self):
        rows = [_row("x", f"p{i}", 0.9, 1.0) for i in range(10)] + [_row("y", "p0", 0.9, 1.0)]
        out = offchain_credit.standing(rows, 1.0, voice_cap=5.0)
        self.assertAlmostEqual(out["y"], 0.45)
        self.assertAlmostEqual(out["x"], 0.9 * 5.0 / 6.0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProofAgeTests(_Db, unittest.TestCase):
    """A node (this one) with a database, and a publisher that signs proofs for it."""

    def setUp(self):
        _Db.setUp(self)
        self.me = _Signer()
        self.me.__enter__()
        self.addCleanup(self.me.__exit__)
        self.publisher = _Signer()

    def _signed(self, signer, opinions, ts=NOW):
        self.me.__exit__()
        try:
            return _proof(signer, opinions, ts)
        finally:
            self.me.__enter__()

    def _age(self, days):
        return patch.object(offchain_proof, "_setting",
                            side_effect=lambda key, default: days
                            if key == "MAX_OFFCHAIN_PROOF_AGE_DAYS" else default)

    def test_a_proof_older_than_the_limit_is_refused(self):
        self.add_peer(self.publisher.peer_id)
        old = self._signed(self.publisher, [_score("ab" * 32, 40)],
                           ts=int(time.time()) - 31 * DAY)
        with self._age(30):
            self.assertEqual(offchain_proof.receive(old.SerializeToString()), "too old")
        self.assertEqual(self.sql.get_offchain_proofs(), [])

    def test_a_proof_inside_the_limit_is_kept(self):
        self.add_peer(self.publisher.peer_id)
        fresh = self._signed(self.publisher, [_score("ab" * 32, 40)], ts=NOW)
        with self._age(30):
            self.assertIsNone(offchain_proof.receive(fresh.SerializeToString()))
        self.assertEqual(len(self.sql.get_offchain_proofs()), 1)

    def test_zero_days_is_no_limit(self):
        self.add_peer(self.publisher.peer_id)
        old = self._signed(self.publisher, [_score("ab" * 32, 40)],
                           ts=int(time.time()) - 900 * DAY)
        with self._age(0):
            self.assertIsNone(offchain_proof.receive(old.SerializeToString()))

    def test_refresh_drops_a_kept_proof_that_got_too_old(self):
        self.add_peer(self.publisher.peer_id)
        self.sql.store_offchain_proof(self.publisher.peer_id, int(time.time()) - 40 * DAY,
                                      b"x")
        with self._age(30), patch("src.reputation_system.onchain_indexer.local_scores",
                                  return_value={}):
            offchain_indexer.refresh()
        self.assertEqual(self.sql.get_offchain_proofs(), [])

    def test_refresh_keeps_a_proof_inside_the_limit(self):
        self.add_peer(self.publisher.peer_id)
        self.sql.store_offchain_proof(self.publisher.peer_id, int(time.time()) - DAY, b"x")
        with self._age(30), patch("src.reputation_system.onchain_indexer.local_scores",
                                  return_value={}):
            offchain_indexer.refresh()
        self.assertEqual(len(self.sql.get_offchain_proofs()), 1)
