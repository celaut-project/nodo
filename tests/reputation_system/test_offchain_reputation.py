"""Off-chain reputation (#652): fault proofs, signed proofs, their weight and transport.

The statements here are built in memory and signed with real identity keys, so
``fault_proofs.check`` runs as it does on a node that received them: with the public key
of the accused node and nothing else.
"""
import json
import sqlite3
import time
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    from mnemonic import Mnemonic

    from protos import celaut_pb2
    from protos import celaut_pb2_grpc as rpc
    from src.balancers.scoring import score
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.gateway.gateway import Gateway
    from src.identity import node_identity, statement_signature
    from src.manager import statement_check
    from src.reputation_system import (
        fault_proofs, offchain_credit, offchain_indexer, offchain_proof,
    )
    from src.reputation_system.contracts.ergo.transaction import add_fault_boxes
    from src.reputation_system.reasons import Reason
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


NOW = int(time.time()) - 3600


class _Signer:
    """An identity: ``with signer:`` makes it the key of this node."""

    def __init__(self):
        self.mnemonic = Mnemonic("english").generate()
        with self:
            self.peer_id = node_identity.get_node_public_key_hex()

    def __enter__(self):
        self._patch = patch.object(node_identity, "get_identity_mnemonic",
                                   return_value=self.mnemonic)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _movement(prev, seq, ts, amount, balance, kind, detail=None, ref=""):
    text = ledger.canonical_detail(detail) or ""
    movement = celaut_pb2.BalanceMovement(
        seq=seq, ts=ts, amount=celaut_pb2.Amount(n=str(amount)),
        balance_after=celaut_pb2.Amount(n=str(balance)), kind=kind, instance_ref=ref,
        detail=text, prev_hash=prev)
    return movement


def _chain(account, rows, start_ts=NOW - 1000):
    """Movements 1..n of ``account``: ``rows`` are ``(amount, kind, detail)``. Each one
    hangs from the one before it and adds to the balance."""
    prev = ledger.genesis_hash(account)
    balance, out = 0, []
    for i, (amount, kind, detail) in enumerate(rows, start=1):
        balance += amount
        movement = _movement(prev, i, start_ts + 10 * i, amount, balance, kind, detail)
        out.append(movement)
        prev = statement_signature.movement_hash(movement)
    return out


def _page(signer, account, movements, *, ts=NOW, instance_ref="", head=None):
    """A signed page with ``movements``. ``head`` is the last movement of the account,
    the last of the page when None."""
    head = head or movements[-1]
    statement = celaut_pb2.BalanceStatement(
        account_ref=ledger.account_ref(account), from_seq=movements[0].seq,
        to_seq=movements[-1].seq, ts=ts, retention_days=0, oldest_seq=1,
        head_seq=head.seq, head_hash=statement_signature.movement_hash(head),
        balance=celaut_pb2.Amount(n=head.balance_after.n), instance_ref=instance_ref)
    statement.movements.extend(movements)
    with signer:
        statement.signature = node_identity.sign_peer_payload(
            node_identity.statement_payload(signer.peer_id,
                                            statement_signature.statement_digest(statement)))
    return statement


def _quote(init=10, most=20, seconds=10, bounded=True, on_close="refuse", closes_at=0):
    terms = {"init_maintenance_cost": init, "max_maintenance_cost": most,
             "maintenance_seconds_loop": seconds, "price_bounded": bounded,
             "price_volatility": 0.0, "volatility_period_seconds": 0,
             "on_close": on_close, "closes_at": closes_at}
    return statement_check.start_quote_detail(
        terms, {"mem_limit": 1024, "disk_space": 2048, "cpu_period": 100000,
                "cpu_quota": 100000})


def _tick(mem=1024, seconds=10):
    return {"seconds": seconds, "mem_limit": mem, "disk_space": 2048,
            "cpu_period": 100000, "cpu_quota": 100000}


def _evidence(reason, accused, statements, receipts=()):
    return fault_proofs.evidence_of(reason, accused, statements, receipts)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FaultProofTests(unittest.TestCase):
    """``fault_proofs.check`` proves a fault from signed pages alone, or proves nothing."""

    def setUp(self):
        self.peer = _Signer()
        self.other = _Signer()

    def test_a_page_that_holds_together_proves_no_inconsistency(self):
        page = _page(self.peer, "c", _chain("c", [(100, "start", None), (-10, "build", None)]))
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [page])))

    def test_a_page_that_does_not_add_up_proves_an_inconsistency(self):
        movements = _chain("c", [(100, "start", None), (-10, "build", None)])
        movements[1].balance_after.n = "95"  # 100 - 10 is not 95
        page = _page(self.peer, "c", movements)

        proven = fault_proofs.check(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [page]))

        self.assertIsNotNone(proven)
        self.assertEqual(proven.reason, Reason.STATEMENT_INCONSISTENT)
        self.assertEqual(proven.seq, 2)
        self.assertEqual(proven.account_ref, ledger.account_ref("c"))

    def test_evidence_signed_by_another_key_proves_nothing(self):
        movements = _chain("c", [(100, "start", None), (-10, "build", None)])
        movements[1].balance_after.n = "95"
        page = _page(self.other, "c", movements)
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [page])))

    def test_a_page_changed_after_the_signature_proves_nothing(self):
        movements = _chain("c", [(100, "start", None), (-10, "build", None)])
        page = _page(self.peer, "c", movements)
        page.movements[1].balance_after.n = "95"
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [page])))

    def test_two_pages_with_two_hashes_for_one_seq_prove_an_inconsistency(self):
        first = _page(self.peer, "c", _chain("c", [(100, "start", None), (-10, "build", None)]))
        second = _page(self.peer, "c", _chain("c", [(100, "start", None), (-20, "build", None)]),
                       ts=NOW + 60)

        proven = fault_proofs.check(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [first, second]))

        self.assertIsNotNone(proven)
        self.assertEqual(proven.seq, 2)

    def test_a_head_that_goes_back_proves_an_inconsistency(self):
        movements = _chain("c", [(100, "start", None), (-10, "build", None),
                                 (-5, "maintenance", _tick())])
        first = _page(self.peer, "c", movements)
        second = _page(self.peer, "c", movements[:2], ts=NOW + 60)

        proven = fault_proofs.check(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [first, second]))

        self.assertIsNotNone(proven)
        self.assertIn("went back", proven.problem)

    def test_pages_of_two_accounts_prove_nothing(self):
        first = _page(self.peer, "c", _chain("c", [(100, "start", None)]))
        second = _page(self.peer, "d", _chain("d", [(50, "start", None)]))
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [first, second])))

    def test_a_stop_that_keeps_the_mu_of_an_orphan_is_proved(self):
        page = _page(self.peer, "c", _chain("c", [
            (100, "start", None), (-10, "maintenance", _tick()),
            (-80, "refund", {"lost": "father_gone"}),
            (0, "stop", {"reason": "process_lost", "leftover": 80}),
        ]))

        proven = fault_proofs.check(_evidence(Reason.KILLED_WITH_MU, self.peer.peer_id, [page]))

        self.assertIsNotNone(proven)
        self.assertEqual(proven.seq, 4)
        self.assertEqual(proven.penalty, statement_check.STATEMENT_PENALTY)

    def test_a_stop_that_gives_the_mu_back_to_the_father_is_not_a_fault(self):
        page = _page(self.peer, "c", _chain("c", [
            (100, "start", None), (-80, "refund", {}),
            (0, "stop", {"reason": "father_stop", "leftover": 80}),
        ]))
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.KILLED_WITH_MU, self.peer.peer_id, [page])))

    def test_maintenance_above_the_signed_quote_is_proved(self):
        page = _page(self.peer, "c", _chain("c", [
            (1000, "start", _quote(init=10)), (-10, "maintenance", _tick()),
            (-50, "maintenance", _tick()), (-50, "maintenance", _tick()),
        ]))

        proven = fault_proofs.check(
            _evidence(Reason.MAINTENANCE_OVERCHARGED, self.peer.peer_id, [page]))

        self.assertIsNotNone(proven)
        self.assertEqual(proven.reason, Reason.MAINTENANCE_OVERCHARGED)

    def test_maintenance_within_the_signed_quote_is_not_a_fault(self):
        page = _page(self.peer, "c", _chain("c", [
            (1000, "start", _quote(init=10)), (-10, "maintenance", _tick()),
            (-10, "maintenance", _tick()), (-10, "maintenance", _tick()),
        ]))
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.MAINTENANCE_OVERCHARGED, self.peer.peer_id, [page])))

    def test_a_start_with_no_quote_proves_no_overcharge(self):
        page = _page(self.peer, "c", _chain("c", [
            (1000, "start", None), (-10, "maintenance", _tick()),
            (-50, "maintenance", _tick()),
        ]))
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.MAINTENANCE_OVERCHARGED, self.peer.peer_id, [page])))

    def test_the_quote_page_and_a_later_page_prove_an_overcharge(self):
        movements = _chain("c", [
            (1000, "start", _quote(init=10)), (-10, "build", None),
            (-10, "maintenance", _tick()), (-60, "maintenance", _tick()),
        ])
        start = _page(self.peer, "c", movements[:2], head=movements[-1])
        later = _page(self.peer, "c", movements[2:], ts=NOW + 10)

        proven = fault_proofs.check(
            _evidence(Reason.MAINTENANCE_OVERCHARGED, self.peer.peer_id, [start, later]))

        self.assertIsNotNone(proven)

    def test_a_hibernation_that_the_quote_did_not_say_is_proved(self):
        page = _page(self.peer, "c", _chain("c", [
            (1000, "start", _quote(on_close="refuse")), (-10, "maintenance", _tick()),
            (-2, "maintenance", _tick(mem=0)),
        ]))

        proven = fault_proofs.check(_evidence(
            Reason.INSTANCE_HIBERNATED_UNANNOUNCED, self.peer.peer_id, [page]))

        self.assertIsNotNone(proven)
        self.assertEqual(proven.penalty, -50)

    def test_a_reason_that_needs_what_only_the_reporter_knows_proves_nothing(self):
        page = _page(self.peer, "c", _chain("c", [(100, "start", None)]))
        self.assertIsNone(fault_proofs.check(
            _evidence(Reason.STATEMENT_OVERCHARGED, self.peer.peer_id, [page])))

    def test_attach_gives_back_only_evidence_that_proves_its_fault(self):
        good = _page(self.peer, "c", _chain("c", [(100, "start", None)]))
        self.assertIsNone(fault_proofs.attach(
            _evidence(Reason.STATEMENT_INCONSISTENT, self.peer.peer_id, [good])))

    def test_the_digest_is_the_same_for_two_copies_of_the_same_evidence(self):
        page = _page(self.peer, "c", _chain("c", [(100, "start", None)]))
        one = _evidence(Reason.KILLED_WITH_MU, self.peer.peer_id, [page])
        two = celaut_pb2.FaultEvidence()
        two.ParseFromString(one.SerializeToString())
        self.assertEqual(fault_proofs.digest(one), fault_proofs.digest(two))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SignedQuoteTests(unittest.TestCase):
    """The quote in the ``start`` movement, as the columns of a delegated row."""

    def test_the_signed_quote_becomes_the_columns_of_the_row(self):
        row = statement_check.rows_of(celaut_pb2.BalanceStatement(movements=_chain(
            "c", [(1000, "start", _quote(init=7, most=9))])))[0]

        columns = statement_check.signed_quote_row(row)

        self.assertEqual(columns["quote_init_maintenance_cost"], 7)
        self.assertEqual(columns["quote_max_maintenance_cost"], 9)
        self.assertEqual(columns["quote_launch_ts"], row.ts)
        self.assertEqual(columns["peer_base_mem_limit"], 1024)
        quote = statement_check.MaintenanceQuote.of_row(columns)
        self.assertEqual(quote.launch_ts, row.ts)

    def test_a_start_with_no_quote_gives_no_columns(self):
        row = statement_check.rows_of(celaut_pb2.BalanceStatement(movements=_chain(
            "c", [(1000, "start", {"execution_name": "ab" * 32})])))[0]
        self.assertIsNone(statement_check.signed_quote_row(row))


class _Db:
    """An in-memory database for the tests that store proofs and evidence."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        create_tables(self.connection.cursor())
        self.connection.commit()
        original = SQLConnection._connection
        SQLConnection._connection = self.connection
        self.addCleanup(setattr, SQLConnection, "_connection", original)
        self.sql = SQLConnection()

    def add_peer(self, peer_id):
        self.connection.execute("INSERT INTO peer (id) VALUES (?)", (peer_id,))
        self.connection.commit()


def _proof(signer, opinions, ts=NOW):
    proof = celaut_pb2.ReputationProof(ts=ts)
    proof.opinions.extend(opinions)
    with signer:
        return offchain_proof.sign(proof)


def _score(subject, amount, positive=True):
    return celaut_pb2.ReputationOpinion(type=offchain_proof.node_type(), object=subject,
                                        amount=amount, positive=positive)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProofSignatureTests(unittest.TestCase):

    def setUp(self):
        self.publisher = _Signer()

    def test_a_signed_proof_verifies(self):
        proof = _proof(self.publisher, [_score("ab" * 32, 40)])
        self.assertEqual(proof.publisher, self.publisher.peer_id)
        self.assertTrue(offchain_proof.verify(proof))

    def test_a_changed_opinion_does_not_verify(self):
        proof = _proof(self.publisher, [_score("ab" * 32, 40)])
        proof.opinions[0].amount = 41
        self.assertFalse(offchain_proof.verify(proof))

    def test_another_publisher_does_not_verify(self):
        proof = _proof(self.publisher, [_score("ab" * 32, 40)])
        proof.publisher = _Signer().peer_id
        self.assertFalse(offchain_proof.verify(proof))

    def test_the_evidence_is_covered_by_the_signature(self):
        accused = _Signer()
        page = _page(accused, "c", _chain("c", [(100, "start", None)]))
        opinion = _score(accused.peer_id, 0, positive=False)
        opinion.evidence.CopyFrom(_evidence(Reason.KILLED_WITH_MU, accused.peer_id, [page]))
        proof = _proof(self.publisher, [opinion])
        proof.opinions[0].evidence.reason = Reason.STATEMENT_INCONSISTENT
        self.assertFalse(offchain_proof.verify(proof))

    def test_scores_leave_out_fault_reports_and_the_publisher_itself(self):
        proof = _proof(self.publisher, [
            _score("ab" * 32, 40), _score("cd" * 32, 0, positive=False),
            _score(self.publisher.peer_id, 99), _score("ef" * 32, 5, positive=False)])
        self.assertEqual(offchain_proof.scores(proof),
                         {"ab" * 32: (40, True), "ef" * 32: (5, False)})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReceiveTests(_Db, unittest.TestCase):
    """What a node does with a proof that a peer sent."""

    def setUp(self):
        _Db.setUp(self)
        self.me = _Signer()
        self.me.__enter__()
        self.addCleanup(self.me.__exit__)
        self.publisher = _Signer()
        self.accused = _Signer()

    def _signed(self, signer, opinions, ts=NOW):
        # The proof is signed with the key of ``signer``; this node is ``me`` again after.
        self.me.__exit__()
        try:
            return _proof(signer, opinions, ts)
        finally:
            self.me.__enter__()

    def test_the_proof_of_a_peer_is_kept_and_an_older_one_is_refused(self):
        self.add_peer(self.publisher.peer_id)
        newer = self._signed(self.publisher, [_score("ab" * 32, 40)], ts=NOW)
        older = self._signed(self.publisher, [_score("ab" * 32, 1)], ts=NOW - 10)

        self.assertIsNone(offchain_proof.receive(newer.SerializeToString()))
        self.assertEqual(offchain_proof.receive(older.SerializeToString()),
                         "not newer than the kept proof")
        self.assertEqual(self.sql.offchain_proof_ts(self.publisher.peer_id), NOW)

    def test_the_proof_of_a_node_that_is_not_a_peer_is_not_kept(self):
        proof = self._signed(self.publisher, [_score("ab" * 32, 40)])
        self.assertIsNone(offchain_proof.receive(proof.SerializeToString()))
        self.assertEqual(self.sql.get_offchain_proofs(), [])

    def test_a_proof_that_does_not_verify_is_refused(self):
        proof = self._signed(self.publisher, [_score("ab" * 32, 40)])
        proof.opinions[0].amount = 7
        self.assertEqual(offchain_proof.receive(proof.SerializeToString()),
                         "not signed by its publisher")

    def test_valid_evidence_scores_the_accused_once_whoever_sends_it(self):
        self.add_peer(self.accused.peer_id)
        page = _page(self.accused, "c", _chain("c", [
            (100, "start", None), (-80, "refund", {"lost": "father_gone"}),
            (0, "stop", {"reason": "process_lost", "leftover": 80})]))
        opinion = _score(self.accused.peer_id, 0, positive=False)
        opinion.evidence.CopyFrom(_evidence(Reason.KILLED_WITH_MU, self.accused.peer_id, [page]))
        first = self._signed(self.publisher, [opinion], ts=NOW)
        again = self._signed(_Signer(), [opinion], ts=NOW)

        with patch("src.reputation_system.interface.update_peer_reputation") as scored:
            offchain_proof.receive(first.SerializeToString())
            offchain_proof.receive(again.SerializeToString())

        scored.assert_called_once_with(peer_id=self.accused.peer_id,
                                       amount=statement_check.STATEMENT_PENALTY,
                                       reason=Reason.KILLED_WITH_MU)
        # The evidence is kept, and this node publishes it in its own proof.
        reports = offchain_proof._fault_opinions(self.sql, self.me.peer_id)
        self.assertEqual([r.object for r in reports], [self.accused.peer_id])
        self.assertTrue(reports[0].HasField("evidence"))

    def test_evidence_against_this_node_is_not_applied(self):
        page = _page(self.me, "c", _chain("c", [
            (100, "start", None), (-80, "refund", {"lost": "father_gone"}),
            (0, "stop", {"reason": "process_lost", "leftover": 80})]))
        evidence = _evidence(Reason.KILLED_WITH_MU, self.me.peer_id, [page])
        with patch("src.reputation_system.interface.update_peer_reputation") as scored:
            self.assertFalse(offchain_proof.apply_evidence(
                evidence, self.me.peer_id, {self.me.peer_id}))
        scored.assert_not_called()

    def test_a_fault_that_this_node_found_first_is_not_scored_again(self):
        self.add_peer(self.accused.peer_id)
        page = _page(self.accused, "c", _chain("c", [
            (100, "start", None), (-80, "refund", {"lost": "father_gone"}),
            (0, "stop", {"reason": "process_lost", "leftover": 80})]))
        evidence = _evidence(Reason.KILLED_WITH_MU, self.accused.peer_id, [page])
        self.assertTrue(statement_check.keep_evidence(evidence, "own"))
        with patch("src.reputation_system.interface.update_peer_reputation") as scored:
            self.assertFalse(offchain_proof.apply_evidence(
                evidence, self.me.peer_id, {self.accused.peer_id}))
        scored.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WeightTests(unittest.TestCase):
    """``w(p) = cred(p) * max(0, r_local(p))`` and ``s = sum w v / (sum w + prior)``."""

    def setUp(self):
        self.publisher = _Signer()

    def test_a_publisher_that_we_never_dealt_with_has_no_voice(self):
        proof = _proof(self.publisher, [_score("ab" * 32, 50)])
        rows = offchain_indexer.opinion_rows([proof], {"ab" * 32: 0.5}, half=50)
        self.assertEqual(rows, [("ab" * 32, self.publisher.peer_id, 0.5, 0.0)])

    def test_a_publisher_that_failed_us_has_no_voice(self):
        proof = _proof(self.publisher, [_score("ab" * 32, 50)])
        rows = offchain_indexer.opinion_rows(
            [proof], {"ab" * 32: 0.5, self.publisher.peer_id: -0.5}, half=50)
        self.assertEqual(rows[0][3], 0.0)

    def test_a_publisher_that_agrees_with_us_and_served_us_well_has_a_voice(self):
        proof = _proof(self.publisher, [_score("ab" * 32, 50), _score("cd" * 32, 50),
                                        _score("ef" * 32, 150)])
        local = {"ab" * 32: 0.5, "cd" * 32: 0.5, self.publisher.peer_id: 0.5}
        rows = {row[0]: row for row in offchain_indexer.opinion_rows([proof], local, half=50)}
        # Full agreement on the subjects that both score, and our score of it is 0.5.
        self.assertAlmostEqual(rows["ef" * 32][3], 0.5)
        self.assertAlmostEqual(rows["ef" * 32][2], 0.75)

    def test_the_standing_is_bounded_and_one_weak_voice_moves_little(self):
        rows = [{"subject_id": "s", "publisher": "p", "verdict": 0.9, "weight": 0.1}]
        self.assertAlmostEqual(offchain_credit.standing(rows, prior_weight=1.0)["s"],
                               0.09 / 1.1)
        strong = [{"subject_id": "s", "publisher": f"p{i}", "verdict": -0.9, "weight": 1.0}
                  for i in range(9)]
        value = offchain_credit.standing(strong, prior_weight=1.0)["s"]
        self.assertLess(value, 0)
        self.assertGreater(value, -1)

    def test_the_local_candidate_gets_no_off_chain_term(self):
        local = score(cost_mu=10, offchain_reputation=1.0, offchain_weight=5.0, local_bias=0.0)
        peer = score(cost_mu=10, offchain_reputation=1.0, offchain_weight=5.0)
        self.assertAlmostEqual(peer - local, 5.0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerFaultBoxTests(unittest.TestCase):
    """Each fault report is a box of one token against the accused (#652)."""

    def test_the_tokens_come_from_the_largest_opinion(self):
        objects = [("a", 60.0, ""), ("b", 39.0, ""), (None, 1, None)]
        out = add_fault_boxes(objects, [("c", '{"reason":"killed_with_mu"}')])
        self.assertEqual(out[0], ("a", 59.0, ""))
        self.assertEqual(out[-1], ("c", -1, '{"reason":"killed_with_mu"}'))
        self.assertEqual(sum(abs(obj[1]) for obj in out), 100)

    def test_no_opinion_with_enough_tokens_publishes_no_fault(self):
        objects = [(None, 1, None)]
        self.assertEqual(add_fault_boxes(objects, [("c", "{}")]), objects)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WiringTests(unittest.TestCase):

    def test_rpc_four_way_wiring_and_actual_gateway_handler(self):
        self.assertTrue(hasattr(rpc.GatewayServicer, "GetReputationProofs"))
        self.assertTrue(hasattr(Gateway, "GetReputationProofs"))
        channel = MagicMock()
        rpc.GatewayStub(channel)
        self.assertIn("/celaut.Gateway/GetReputationProofs",
                      [c.args[0] for c in channel.stream_stream.call_args_list])
        server = MagicMock()
        rpc.add_GatewayServicer_to_server(MagicMock(), server)
        handlers = server.add_generic_rpc_handlers.call_args.args[0][0]
        self.assertIn("/celaut.Gateway/GetReputationProofs", handlers._method_handlers)

    def test_sharing_off_serves_nothing(self):
        with patch.object(offchain_proof, "share_proofs", return_value=False):
            self.assertEqual(list(offchain_proof.serve()), [])


if __name__ == "__main__":
    unittest.main()
