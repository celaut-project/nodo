"""The setting that keeps the evidence of our own faults off our proof (#658)."""
import unittest
from unittest.mock import patch

from tests.reputation_system.test_offchain_reputation import (
    IMPORT_ERROR, _Db, _Signer, _chain, _evidence, _page,
)

if IMPORT_ERROR is None:
    from src.reputation_system import fault_proofs, offchain_proof
    from src.reputation_system.reasons import Reason


def _setting(**values):
    return patch.object(offchain_proof, "_setting",
                        side_effect=lambda key, default: values.get(key, default))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FaultEvidencePrivacyTests(_Db, unittest.TestCase):

    def setUp(self):
        _Db.setUp(self)
        self.me = _Signer()
        self.accused = _Signer()
        self.other = _Signer()
        self.add_peer(self.accused.peer_id)
        self.add_peer(self.other.peer_id)

    def _keep(self, signer, source, account="c"):
        page = _page(signer, account, _chain(account, [
            (100, "start", None), (-80, "refund", {"lost": "father_gone"}),
            (0, "stop", {"reason": "process_lost", "leftover": 80})]))
        evidence = _evidence(Reason.KILLED_WITH_MU, signer.peer_id, [page])
        proven = fault_proofs.check(evidence)
        self.assertIsNotNone(proven)
        self.sql.record_fault_evidence(
            signer.peer_id, evidence.reason, proven.account_ref, proven.seq,
            fault_proofs.digest(evidence), evidence.SerializeToString(), source)
        return evidence

    def _reports(self):
        return {o.object: o for o in
                offchain_proof._fault_opinions(self.sql, self.me.peer_id)}

    def test_by_default_the_evidence_of_our_own_fault_is_published(self):
        self._keep(self.accused, "own")
        with _setting():
            self.assertTrue(self._reports()[self.accused.peer_id].HasField("evidence"))

    def test_with_the_setting_off_our_own_fault_goes_with_no_evidence(self):
        self._keep(self.accused, "own")
        with _setting(PUBLISH_FAULT_EVIDENCE=False):
            report = self._reports()[self.accused.peer_id]
        self.assertFalse(report.HasField("evidence"))
        self.assertEqual(report.object, self.accused.peer_id)
        self.assertIn(b"killed_with_mu", bytes(report.content))

    def test_evidence_that_we_received_is_public_already_and_still_goes_on(self):
        self._keep(self.other, "received", account="d")
        with _setting(PUBLISH_FAULT_EVIDENCE=False):
            self.assertTrue(self._reports()[self.other.peer_id].HasField("evidence"))

    def test_the_fault_is_not_reported_twice_when_the_evidence_is_withheld(self):
        self._keep(self.accused, "own")
        with _setting(PUBLISH_FAULT_EVIDENCE=False):
            reports = offchain_proof._fault_opinions(self.sql, self.me.peer_id)
        self.assertEqual(len(reports), 1)

    def test_no_fault_box_goes_on_the_ledger_for_withheld_evidence(self):
        self._keep(self.accused, "own")
        self._keep(self.other, "received", account="d")
        with _setting(PUBLISH_FAULT_EVIDENCE=False, MAX_FAULT_REPORTS_ON_LEDGER=8):
            accused = [a for a, _ in offchain_proof.ledger_fault_reports()]
        self.assertEqual(accused, [self.other.peer_id])
        with _setting(MAX_FAULT_REPORTS_ON_LEDGER=8):
            accused = {a for a, _ in offchain_proof.ledger_fault_reports()}
        self.assertEqual(accused, {self.accused.peer_id, self.other.peer_id})
