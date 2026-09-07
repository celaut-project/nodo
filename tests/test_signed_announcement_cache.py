"""Signing a `GetPeerInfo` answer once per change, not once per caller (issue #304).

A signed announcement is a public object: `manager._passes_anti_replay` says so in as
many words -- `ts` guards only against a downgrade to a stale address, nothing from the
caller enters the signed payload, and the claim is safe to accept from anyone relaying
it. So a fresh signature per caller buys nothing, on an RPC that is unauthenticated and
callable at any rate. These pin that it is not made, and that nothing is given up for it.
"""
import time
import unittest
import unittest.mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.gateway import utils as gateway_utils
    from src.identity import node_identity as ni
    from src.reputation_system import fetch as reputation_fetch
    from src.reputation_system import proof_attestation
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

PROOF_ID = "46bf6503dfa0551e7a74f005f33b717f26115ed21f338297639040d3d0cfe484"
WALLET_MNEMONIC = (
    "ozone drill grab fiber curtain grace pudding thank cruise elder eight picnic"
)


def _announcement(ip="1.2.3.4", port=8080):
    peer = celaut_pb2.Peer()
    uri = peer.uri.add(ip=ip, port=port)
    uri.transport.tags.append("tcp")
    return peer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SignedAnnouncementCacheTests(unittest.TestCase):
    def setUp(self):
        gateway_utils._signed_peers.clear()
        self.addCleanup(gateway_utils._signed_peers.clear)

    def _counting_signer(self):
        """Wraps the real signer so the count is of real signatures, not of stubs."""
        calls = []
        real = ni.sign_peer_payload
        patch = unittest.mock.patch.object(
            ni, "sign_peer_payload", lambda p: (calls.append(p), real(p))[1]
        )
        return calls, patch

    def test_two_identical_announcements_are_signed_once(self):
        calls, patch = self._counting_signer()
        with patch:
            first, second = _announcement(), _announcement()
            gateway_utils._sign_peer(first)
            gateway_utils._sign_peer(second)

        self.assertEqual(len(calls), 1)
        self.assertEqual(first.SerializeToString(), second.SerializeToString())

    def test_a_cached_answer_still_verifies(self):
        # The point of caching a signature is that it stays a valid one: what is served
        # the second time has to check out exactly as the first did.
        peer = _announcement()
        gateway_utils._sign_peer(peer)
        served = _announcement()
        gateway_utils._sign_peer(served)

        self.assertTrue(ni.verify_peer_payload(
            served.public_key,
            ni.canonical_peer_payload(
                served.public_key, served.ts, ni.canonical_peer_content_digest(served)
            ),
            served.signature,
        ))

    def test_changed_content_is_signed_again(self):
        # The cache is keyed on the content digest, which already covers every field
        # that distinguishes one announcement from another -- so a new address
        # invalidates it with nothing here remembering to.
        calls, patch = self._counting_signer()
        with patch:
            gateway_utils._sign_peer(_announcement())
            gateway_utils._sign_peer(_announcement(ip="9.9.9.9"))

        self.assertEqual(len(calls), 2)

    def test_a_declaration_change_is_signed_again(self):
        # Not just the addresses: the digest covers the payment contracts, the rates,
        # the proofs, the signature scheme and each address's protocol stack, so a
        # change in any of them has to reach the wire.
        calls, patch = self._counting_signer()
        with patch:
            gateway_utils._sign_peer(_announcement())
            with_rate = _announcement()
            with_rate.mu_per_call["exec"].n = "10"
            gateway_utils._sign_peer(with_rate)

        self.assertEqual(len(calls), 2)

    def test_the_cache_expires(self):
        # `ts` is frozen while an answer is re-served, and each URI's advertised expiry
        # counts forward from it, so a cached answer must not be servable forever.
        calls, patch = self._counting_signer()
        with patch:
            gateway_utils._sign_peer(_announcement())
            stale = time.monotonic() + gateway_utils._signed_peer_ttl() + 1
            with unittest.mock.patch.object(time, "monotonic", lambda: stale):
                gateway_utils._sign_peer(_announcement())

        self.assertEqual(len(calls), 2)

    def test_the_ttl_shrinks_with_the_advertised_validity(self):
        # A node telling peers its address is good for ten minutes must not re-serve a
        # ts that has already eaten a meaningful slice of that.
        with unittest.mock.patch.object(
            gateway_utils.env_manager, "get",
            side_effect=lambda k, d=None: 600 if k == "network.ADDRESS_VALIDITY_SECONDS" else d,
        ):
            self.assertLessEqual(gateway_utils._signed_peer_ttl(), 60.0)
            self.assertGreater(gateway_utils._signed_peer_ttl(), 0)

    def test_no_advertised_expiry_leaves_the_ttl_alone(self):
        # The default: nothing in the message ages with `ts`, so the bound is only the
        # staleness one.
        with unittest.mock.patch.object(
            gateway_utils.env_manager, "get",
            side_effect=lambda k, d=None: 0 if k == "network.ADDRESS_VALIDITY_SECONDS" else d,
        ):
            self.assertEqual(gateway_utils._signed_peer_ttl(), 60.0)

    def test_concurrent_first_calls_sign_once(self):
        # gRPC serves GetPeerInfo on several threads, so without the lock two callers
        # arriving together each pay for a signature.
        import threading

        calls, patch = self._counting_signer()
        with patch:
            start = threading.Barrier(4)

            def announce():
                start.wait()
                gateway_utils._sign_peer(_announcement())

            threads = [threading.Thread(target=announce) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(calls), 1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TwoAnnouncementShapesShareTheCacheTests(unittest.TestCase):
    """This node announces itself in more than one shape (issue #329).

    `generate_full_node_peer_info` answers GetPeerInfo with every interface;
    `generate_node_peer_info` builds one bridge address per service launch. Their
    digests differ, so a cache with a single slot had each call evicting the other's
    entry, and a node launching instances while serving GetPeerInfo missed every time --
    the case the cache exists for.
    """

    def setUp(self):
        gateway_utils._signed_peers.clear()
        self.addCleanup(gateway_utils._signed_peers.clear)

    _counting_signer = SignedAnnouncementCacheTests._counting_signer

    def test_two_shapes_do_not_evict_each_other(self):
        calls, patch = self._counting_signer()
        with patch:
            for _ in range(2):
                gateway_utils._sign_peer(_announcement(ip="1.2.3.4"))
                gateway_utils._sign_peer(_announcement(ip="10.0.0.1", port=9090))

        # Two shapes, one signature each, however they interleave.
        self.assertEqual(len(calls), 2)

    def test_the_cache_is_bounded(self):
        calls, patch = self._counting_signer()
        with patch:
            for i in range(gateway_utils._SIGNED_PEERS_MAX + 3):
                gateway_utils._sign_peer(_announcement(ip=f"10.0.0.{i}"))

        self.assertLessEqual(
            len(gateway_utils._signed_peers), gateway_utils._SIGNED_PEERS_MAX
        )

    def test_the_oldest_shape_is_the_one_dropped(self):
        # Asked of the behaviour rather than of the dict: what a caller can observe is
        # whether re-serving a shape costs a signature.
        calls, patch = self._counting_signer()
        newest = f"10.0.0.{gateway_utils._SIGNED_PEERS_MAX}"
        with patch:
            for i in range(gateway_utils._SIGNED_PEERS_MAX + 1):
                gateway_utils._sign_peer(_announcement(ip=f"10.0.0.{i}"))
            so_far = len(calls)

            gateway_utils._sign_peer(_announcement(ip=newest))
            self.assertEqual(len(calls), so_far, "the newest shape was not kept")

            gateway_utils._sign_peer(_announcement(ip="10.0.0.0"))
            self.assertEqual(len(calls), so_far + 1, "the oldest shape was not dropped")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AttestedProofsDoNotMoveTheDigestTests(unittest.TestCase):
    """An announced reputation proof has to describe the same content twice (issue #314).

    The cache above is keyed on the announcement's content digest, and a proof's owner
    attestation is an xattr the digest covers. Ergo's Schnorr signing draws a random
    nonce, so an attestation signed per announcement gives identical content a different
    digest, and the cache then never hits on exactly the nodes that hold a proof. These
    go through `local_proofs`, where the attestation is made: a hand-built `Peer` never
    reaches it, so a case that builds one cannot see this.
    """

    _counting_signer = SignedAnnouncementCacheTests._counting_signer

    def setUp(self):
        gateway_utils._signed_peers.clear()
        self.addCleanup(gateway_utils._signed_peers.clear)
        proof_attestation._owner_attestation.cache_clear()
        self.addCleanup(proof_attestation._owner_attestation.cache_clear)

        # Only the two keys, everything else through to the real config. ConfigManager
        # is a Singleton, so this object is the one the whole process reads: a mock that
        # answered `default` to anything unlisted would also blank out
        # `identity.MNEMONIC`, leaving the node with no peer_id to attest and nothing to
        # sign -- which looks exactly like the cache working.
        real_get = reputation_fetch.env_manager.get

        def config(key, default=None):
            if key == "ledgers.ergo.reputation.REPUTATION_PROOF_ID":
                return PROOF_ID
            if key == "ledgers.ergo.WALLET_MNEMONIC":
                return WALLET_MNEMONIC
            return real_get(key, default)

        patch = unittest.mock.patch.object(reputation_fetch.env_manager, "get", config)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _announcement_with_proofs(ip="1.2.3.4", port=8080):
        """An announcement carrying this node's proofs, as `_build_peer` assembles it."""
        peer = _announcement(ip=ip, port=port)
        peer.reputation_proofs.extend(reputation_fetch.local_proofs())
        return peer

    def test_the_proof_is_attested(self):
        # Guards the rest: an unattested proof carries no signature xattr, so the
        # digests below would match for the wrong reason.
        peer = self._announcement_with_proofs()
        self.assertEqual(len(peer.reputation_proofs), 1)
        self.assertEqual(
            proof_attestation.attested_proof_owner(
                peer.reputation_proofs[0], ni.get_node_public_key_hex()
            ),
            proof_attestation._wallet_public_key_hex(WALLET_MNEMONIC),
        )

    def test_identical_content_keeps_one_digest(self):
        digests = {
            ni.canonical_peer_content_digest(self._announcement_with_proofs())
            for _ in range(3)
        }
        self.assertEqual(len(digests), 1, f"content digest moved: {digests}")

    def test_an_announcement_with_a_proof_is_signed_once(self):
        calls, patch = self._counting_signer()
        with patch:
            gateway_utils._sign_peer(self._announcement_with_proofs())
            gateway_utils._sign_peer(self._announcement_with_proofs())

        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
