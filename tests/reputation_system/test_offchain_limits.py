"""Limits of GetReputationProofs and PushReputationProof (#652): bytes and calls of one
call, of one client and of all clients; and the push, in both directions."""
import unittest
from unittest.mock import MagicMock, patch

from tests.reputation_system.test_offchain_reputation import (
    IMPORT_ERROR, NOW, _Db, _Signer, _proof, _score,
)

if IMPORT_ERROR is None:
    from protos import celaut_pb2
    from src.gateway.client_gate import ClientRequired
    from src.gateway.iterables import push_reputation_proof_iterable as push_module
    from src.gateway.iterables import reputation_proofs_iterable as pull_module
    from src.reputation_system import offchain_indexer, offchain_limits, offchain_proof


def _settings(**values):
    merged = dict(offchain_limits.DEFAULTS)
    merged.update(values)

    def get(key, default=None):
        name = key.split(".", 1)[1] if key.startswith("reputation.") else key
        return merged.get(name, default)

    return patch.object(offchain_limits.env_manager, "get", side_effect=get)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MeterTests(unittest.TestCase):

    def test_take_stops_at_the_limit_and_the_window_slides(self):
        clock = _Clock()
        meter = offchain_limits.Meter(60, clock=clock)
        self.assertTrue(meter.take("a", 2))
        self.assertTrue(meter.take("a", 2))
        self.assertFalse(meter.take("a", 2))
        self.assertTrue(meter.take("b", 2))
        clock.now += 61
        self.assertTrue(meter.take("a", 2))

    def test_a_limit_of_zero_refuses_all(self):
        self.assertFalse(offchain_limits.Meter(60).take("a", 0))

    def test_used_counts_the_amounts_in_the_window(self):
        clock = _Clock()
        meter = offchain_limits.Meter(10, clock=clock)
        meter.add("a", 5)
        clock.now += 5
        meter.add("a", 7)
        self.assertEqual(meter.used("a"), 12)
        clock.now += 6
        self.assertEqual(meter.used("a"), 7)

    def test_the_memory_is_capped(self):
        meter = offchain_limits.Meter(60)
        with patch.object(offchain_limits, "MAX_TRACKED_CLIENTS", 3):
            for i in range(10):
                meter.add(str(i), 1)
        self.assertLessEqual(len(meter._events), 3)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AdmitTests(unittest.TestCase):

    def setUp(self):
        offchain_limits.reset()
        self.addCleanup(offchain_limits.reset)

    def test_a_client_has_a_number_of_pulls_an_hour(self):
        with _settings(PULL_CALLS_PER_CLIENT_PER_HOUR=2):
            offchain_limits.admit_pull("c")
            offchain_limits.admit_pull("c")
            with self.assertRaises(offchain_limits.Refused):
                offchain_limits.admit_pull("c")
            offchain_limits.admit_pull("other")

    def test_all_clients_together_have_a_number_of_pulls_a_minute(self):
        with _settings(PULL_GLOBAL_CALLS_PER_MINUTE=2):
            offchain_limits.admit_pull("a")
            offchain_limits.admit_pull("b")
            with self.assertRaises(offchain_limits.Refused):
                offchain_limits.admit_pull("c")

    def test_the_bytes_of_a_pull_are_the_least_of_the_response_and_what_is_left(self):
        with _settings(MAX_OFFCHAIN_BYTES_PER_RESPONSE=1000, PULL_BYTES_PER_CLIENT_PER_HOUR=1500):
            self.assertEqual(offchain_limits.admit_pull("c"), 1000)
            offchain_limits.charge_pull("c", 1000)
            self.assertEqual(offchain_limits.admit_pull("c"), 500)
            offchain_limits.charge_pull("c", 500)
            with self.assertRaises(offchain_limits.Refused):
                offchain_limits.admit_pull("c")

    def test_push_is_refused_when_it_is_off_or_from_a_stranger(self):
        with _settings(ACCEPT_OFFCHAIN_PUSH=False):
            with self.assertRaises(offchain_limits.Refused):
                offchain_limits.admit_push("c", True)
        with _settings():
            with self.assertRaises(offchain_limits.Refused):
                offchain_limits.admit_push("c", False)
            with self.assertRaises(offchain_limits.Refused):
                offchain_limits.admit_push("c", None)
            self.assertGreater(offchain_limits.admit_push("c", True), 0)
        with _settings(PUSH_ONLY_FROM_PEERS=False):
            self.assertGreater(offchain_limits.admit_push("stranger", False), 0)

    def test_push_calls_and_bytes_are_limited(self):
        with _settings(PUSH_CALLS_PER_CLIENT_PER_HOUR=2, PUSH_BYTES_PER_CLIENT_PER_DAY=100):
            self.assertEqual(offchain_limits.admit_push("c", True), 100)
            offchain_limits.charge_push("c", 60)
            self.assertEqual(offchain_limits.admit_push("c", True), 40)
            with self.assertRaises(offchain_limits.Refused):
                offchain_limits.admit_push("c", True)

    def test_a_bad_value_falls_back_to_the_default(self):
        with _settings(PULL_CALLS_PER_CLIENT_PER_HOUR="many"):
            self.assertEqual(offchain_limits.setting("PULL_CALLS_PER_CLIENT_PER_HOUR"),
                             offchain_limits.DEFAULTS["PULL_CALLS_PER_CLIENT_PER_HOUR"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServeBytesTests(_Db, unittest.TestCase):

    def setUp(self):
        super().setUp()
        offchain_limits.reset()
        offchain_proof.forget_own_proof()
        self.me = _Signer()
        self.sizes = {}
        for i in range(6):
            publisher = _Signer()
            self.add_peer(publisher.peer_id)
            proof = _proof(publisher, [celaut_pb2.ReputationOpinion(
                type="x", object="ab" * 32, amount=1, content=b"z" * 10_000)], ts=NOW + i)
            raw = proof.SerializeToString()
            self.sizes[publisher.peer_id] = len(raw)
            with self.me:
                self.assertIsNone(offchain_proof.receive(raw))

    def tearDown(self):
        offchain_proof.forget_own_proof()

    def test_the_answer_stays_within_the_bytes(self):
        with self.me, _settings():
            own = offchain_proof.own_proof().ByteSize()
            out = list(offchain_proof.serve(max_bytes=own + 25_000))
        # The own proof, then only the proofs that fit: two of about 10 KB.
        self.assertEqual(out[0].publisher, self.me.peer_id)
        self.assertLessEqual(sum(p.ByteSize() for p in out), own + 25_000)
        self.assertEqual(len(out), 3)

    def test_the_own_proof_goes_also_when_it_is_over_the_bytes(self):
        with self.me, _settings():
            out = list(offchain_proof.serve(max_bytes=1))
        self.assertEqual([p.publisher for p in out], [self.me.peer_id])

    def test_the_default_is_the_bytes_of_the_response(self):
        with self.me, _settings(MAX_OFFCHAIN_BYTES_PER_RESPONSE=0):
            out = list(offchain_proof.serve())
        self.assertEqual(len(out), 1)

    def test_it_reads_only_the_proofs_that_it_sends(self):
        reads = []
        original = type(self.sql).get_offchain_proof

        def counting(sql, publisher):
            reads.append(publisher)
            return original(sql, publisher)

        with self.me, _settings(), patch.object(type(self.sql), "get_offchain_proof", counting), \
                patch.object(type(self.sql), "get_offchain_proofs",
                             side_effect=AssertionError("reads every proof")):
            own = offchain_proof.own_proof().ByteSize()
            out = list(offchain_proof.serve(max_bytes=own + 12_000))
        self.assertEqual(len(out), 2)
        self.assertEqual(len(reads), 1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PullIterableTests(unittest.TestCase):

    def setUp(self):
        offchain_limits.reset()
        self.addCleanup(offchain_limits.reset)

    def _call(self, caller="c"):
        context = MagicMock()
        context.is_active.return_value = True
        with patch.object(pull_module.BeeClient, "parse_one",
                          return_value=celaut_pb2.Client(client_id=caller)), \
                patch.object(pull_module, "require_caller", return_value=caller):
            return list(pull_module.ReputationProofsIterable(iter(()), context))

    def test_a_client_over_the_limit_gets_an_error_and_no_proofs(self):
        with _settings(PULL_CALLS_PER_CLIENT_PER_HOUR=1), \
                patch.object(offchain_proof, "serve", return_value=iter(())):
            self._call()
            with self.assertRaises(ClientRequired):
                self._call()

    def test_the_bytes_sent_are_charged_to_the_client(self):
        proof = celaut_pb2.ReputationProof(publisher="p", ts=1)
        with _settings(), patch.object(offchain_proof, "serve", return_value=iter([proof, proof])):
            self._call("c")
        self.assertEqual(offchain_limits._pull_bytes.used("c"), 2 * proof.ByteSize())

    def test_serve_gets_the_bytes_left(self):
        with _settings(MAX_OFFCHAIN_BYTES_PER_RESPONSE=500), \
                patch.object(offchain_proof, "serve", return_value=iter(())) as served:
            self._call()
        served.assert_called_once_with(500)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PushIterableTests(_Db, unittest.TestCase):

    def setUp(self):
        super().setUp()
        offchain_limits.reset()
        self.addCleanup(offchain_limits.reset)
        self.me = _Signer()
        self.publisher = _Signer()
        self.add_peer(self.publisher.peer_id)
        self.proof = _proof(self.publisher, [_score("ab" * 32, 40)])

    def _push(self, proof, client_id="client", peer_of_client=True):
        context = MagicMock()
        patches = [
            patch.object(push_module, "parse_with_client", return_value=(proof, client_id)),
            patch.object(push_module, "require_caller", return_value=client_id),
            patch.object(push_module.sc, "get_peer_id_by_local_client",
                         return_value="peer" if peer_of_client else None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.me:
            return push_module.PushReputationProofIterable(iter(()), context)._answer(
                proof, client_id, client_id)

    def test_a_proof_from_a_peer_is_taken_and_kept(self):
        with _settings():
            ack = self._push(self.proof)
        self.assertTrue(ack.accepted, ack.reason)
        self.assertEqual(self.sql.offchain_proof_ts(self.publisher.peer_id), self.proof.ts)

    def test_a_stranger_is_refused_before_the_proof_is_checked(self):
        with _settings(), patch.object(offchain_proof, "receive",
                                       side_effect=AssertionError("checked")):
            ack = self._push(self.proof, peer_of_client=False)
        self.assertFalse(ack.accepted)
        self.assertIn("peers only", ack.reason)

    def test_a_call_with_no_proof_is_refused(self):
        with _settings():
            ack = self._push(None)
        self.assertFalse(ack.accepted)

    def test_an_invalid_proof_is_refused_and_its_bytes_count(self):
        bad = celaut_pb2.ReputationProof()
        bad.CopyFrom(self.proof)
        bad.signature = "00" * 64
        with _settings():
            ack = self._push(bad)
        self.assertFalse(ack.accepted)
        self.assertEqual(offchain_limits._push_bytes.used("client"), bad.ByteSize())

    def test_a_proof_over_the_bytes_left_is_refused_unchecked(self):
        with _settings(PUSH_BYTES_PER_CLIENT_PER_DAY=10), \
                patch.object(offchain_proof, "receive", side_effect=AssertionError("checked")):
            ack = self._push(self.proof)
        self.assertFalse(ack.accepted)

    def test_a_client_has_a_number_of_pushes_an_hour(self):
        with _settings(PUSH_CALLS_PER_CLIENT_PER_HOUR=1):
            self.assertTrue(self._push(self.proof).accepted)
            self.assertFalse(self._push(self.proof).accepted)

    def test_a_node_that_does_not_accept_push_refuses_every_call(self):
        with _settings(ACCEPT_OFFCHAIN_PUSH=False):
            ack = self._push(self.proof)
        self.assertFalse(ack.accepted)
        self.assertIsNone(self.sql.offchain_proof_ts(self.publisher.peer_id))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PushTickTests(unittest.TestCase):

    def setUp(self):
        offchain_indexer._last_push = None
        offchain_indexer._pushed_faults = None
        self.addCleanup(setattr, offchain_indexer, "_last_push", None)
        self.addCleanup(setattr, offchain_indexer, "_pushed_faults", None)
        self.proof = celaut_pb2.ReputationProof(publisher="p", ts=1)
        self.fault = celaut_pb2.ReputationOpinion(
            type="x", object="ab" * 32, amount=0, positive=False, content=b'{"reason":"r"}')

    def _tick(self, proof, peers=("a", "b", "c", "d"), **config):
        values = {"reputation.PUSH_OFFCHAIN_PROOFS": True, **config}
        sql = MagicMock()
        sql.return_value.get_peers_id.return_value = list(peers)
        with patch.object(offchain_indexer.ConfigManager, "get",
                          side_effect=lambda k, d=None: values.get(k, d)), \
                patch.object(offchain_proof, "own_proof", return_value=proof), \
                patch("src.database.sql_connection.SQLConnection", sql), \
                patch.object(offchain_indexer, "push_once", return_value=True) as pushed:
            taken = offchain_indexer.push_tick()
        return taken, pushed

    def test_it_pushes_to_the_fanout_when_there_is_a_new_fault(self):
        self.proof.opinions.append(self.fault)
        taken, pushed = self._tick(self.proof, **{"reputation.PUSH_OFFCHAIN_FANOUT": 2})
        self.assertEqual((taken, pushed.call_count), (2, 2))

    def test_it_does_not_push_the_same_faults_twice(self):
        self.proof.opinions.append(self.fault)
        self._tick(self.proof, **{"reputation.PUSH_OFFCHAIN_MIN_INTERVAL_SECONDS": 0})
        taken, pushed = self._tick(self.proof, **{"reputation.PUSH_OFFCHAIN_MIN_INTERVAL_SECONDS": 0})
        self.assertEqual((taken, pushed.call_count), (0, 0))

    def test_it_waits_the_interval_after_a_push(self):
        self.proof.opinions.append(self.fault)
        self._tick(self.proof)
        other = celaut_pb2.ReputationProof()
        other.CopyFrom(self.proof)
        other.opinions.append(celaut_pb2.ReputationOpinion(
            type="x", object="cd" * 32, amount=0, positive=False, content=b'{"reason":"s"}'))
        taken, pushed = self._tick(other)
        self.assertEqual(pushed.call_count, 0)

    def test_it_is_off_with_the_switch_or_a_fanout_of_zero(self):
        self.proof.opinions.append(self.fault)
        self.assertEqual(self._tick(self.proof, **{"reputation.PUSH_OFFCHAIN_PROOFS": False})[0], 0)
        self.assertEqual(self._tick(self.proof, **{"reputation.PUSH_OFFCHAIN_FANOUT": 0})[0], 0)


if __name__ == "__main__":
    unittest.main()
