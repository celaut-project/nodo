"""Fault proofs on the real output of the node (#664, follow-up of #656).

The other fault-proof tests use pages that are built by hand: each tick is one loop, with
no lag. Here the pages come from the code of the node: the quote of the ``start`` row is
made by ``start_quote.local_terms`` and ``start_quote_detail`` (as ``local_execution``
does), the ticks are written by ``move_balance`` with ``tick_detail`` (as ``maintain``
does), and each page is the answer of ``balance_statement`` over the real SQL and the real
signature. The pages go through ``attach`` and ``check``.
"""
import unittest

from tests.reputation_system.test_offchain_reputation import (
    IMPORT_ERROR, _Db, _Signer, _evidence,
)

if IMPORT_ERROR is None:
    from protos import celaut_pb2
    from src.database import ledger
    from src.manager import balance_statement, statement_check
    from src.reputation_system import fault_proofs
    from src.reputation_system.reasons import Reason
    from src.utils.cost_functions import start_quote

SHAPE = {"mem_limit": 512 * 2**20, "disk_space": 2**30, "cpu_period": 100000,
         "cpu_quota": 100000}
LOOP = 10
# Every reason that the evidence of a page can prove.
PAGE_REASONS = (Reason.STATEMENT_INCONSISTENT, Reason.KILLED_WITH_MU,
                Reason.MAINTENANCE_OVERCHARGED, Reason.INSTANCE_HIBERNATED_UNANNOUNCED,
                Reason.INSTANCE_STOPPED_UNANNOUNCED)


def _real_start_detail() -> dict:
    """``detail`` of the ``start`` row as ``local_execution._local_quote`` writes it."""
    reserved = celaut_pb2.Sysresources(**SHAPE)
    terms = start_quote.local_terms(
        reserved, reserved, {"mem": 10_000, "cpu": 10_000, "disk": 10_000}, None, None,
        "refuse", 0)
    terms["maintenance_seconds_loop"] = LOOP
    terms["price_bounded"] = True
    terms["price_volatility"] = 0.0
    terms["volatility_period_seconds"] = 0
    terms["init_maintenance_cost"] = terms["max_maintenance_cost"] = 10
    return statement_check.start_quote_detail(terms, start_quote.shape_of(reserved))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RealPagesProveNothingAgainstAnHonestNode(_Db, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.node = _Signer()
        with self.node:
            self.sql.add_client("father", 10**12, None)
            self.sql.add_local_instance(
                father_id="father", container_ip="10.0.0.2", container_id="inst", name="i",
                balance_mu=10_000, serialized_instance="", service_id="ab" * 32,
                virtualizer="ch", disk_space=0, envs="",
                movements=(ledger.Movement(ledger.START, 10_000, detail=_real_start_detail()),))

    def _tick(self, loops=1, mu=10):
        """A tick of ``loops`` loops (a lagged tick charges more than one)."""
        self.sql.move_balance("inst", -mu * loops, ledger.MAINTENANCE,
                              detail=statement_check.tick_detail(SHAPE, LOOP * loops))

    def _page(self, from_seq=1):
        with self.node:
            return balance_statement._answer_account("father", "inst", from_seq, 0, False,
                                                     with_dead=True)

    def test_the_real_start_row_holds_a_quote_that_can_be_read(self):
        page = self._page()
        row = statement_check.rows_of(page)[0]
        self.assertIsNotNone(statement_check.signed_quote_row(row))

    def test_an_honest_page_with_lagged_ticks_proves_no_fault(self):
        self._tick()
        self._tick(loops=3)
        self._tick()
        page = self._page()
        for reason in PAGE_REASONS:
            with self.subTest(reason=reason):
                self.assertIsNone(fault_proofs.check(
                    _evidence(reason, self.node.peer_id, [page])))

    def test_two_honest_pages_of_different_times_prove_no_fault(self):
        self._tick()
        first = self._page()
        self._tick(loops=2)
        self._tick()
        second = self._page()
        later = self._page(from_seq=first.head_seq + 1)
        for reason in PAGE_REASONS:
            for pages in ([first, second], [first, later]):
                with self.subTest(reason=reason, pages=len(pages)):
                    self.assertIsNone(fault_proofs.check(
                        _evidence(reason, self.node.peer_id, pages)))

    def test_a_real_overcharge_is_proven_and_attached(self):
        self._tick()
        self._tick(mu=500)
        pages = [self._page(from_seq=1)]
        evidence = _evidence(Reason.MAINTENANCE_OVERCHARGED, self.node.peer_id, pages)
        proven = fault_proofs.check(evidence)
        self.assertIsNotNone(proven)
        self.assertEqual(proven.accused, self.node.peer_id)
        self.assertIs(fault_proofs.attach(evidence), evidence)

    def test_the_evidence_survives_the_wire(self):
        self._tick()
        self._tick(mu=500)
        evidence = _evidence(Reason.MAINTENANCE_OVERCHARGED, self.node.peer_id,
                             [self._page()])
        copy = celaut_pb2.FaultEvidence()
        copy.ParseFromString(evidence.SerializeToString())
        self.assertIsNotNone(fault_proofs.check(copy))
        self.assertEqual(fault_proofs.digest(copy), fault_proofs.digest(evidence))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DigestEscapingTests(unittest.TestCase):

    def _digest(self, reason, accused):
        return fault_proofs.digest(celaut_pb2.FaultEvidence(reason=reason, accused=accused))

    def test_a_separator_in_a_field_does_not_move_it_to_the_next(self):
        self.assertNotEqual(self._digest("a|b", "c"), self._digest("a", "b|c"))

    def test_an_escape_character_does_not_join_two_evidences(self):
        self.assertNotEqual(self._digest("a\\", "|b"), self._digest("a", "\\|b"))

    def test_a_plain_evidence_keeps_the_digest_that_it_had(self):
        import hashlib
        expected = hashlib.blake2b(b"killed_with_mu|" + b"ab" * 32,
                                   digest_size=32).hexdigest()
        self.assertEqual(self._digest("killed_with_mu", "ab" * 32), expected)
