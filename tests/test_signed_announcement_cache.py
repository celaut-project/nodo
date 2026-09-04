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
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _announcement(ip="1.2.3.4", port=8080):
    peer = celaut_pb2.Peer()
    uri = peer.uri.add(ip=ip, port=port)
    uri.transport.tags.append("tcp")
    return peer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SignedAnnouncementCacheTests(unittest.TestCase):
    def setUp(self):
        gateway_utils._signed_peer = None
        self.addCleanup(setattr, gateway_utils, "_signed_peer", None)

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


if __name__ == "__main__":
    unittest.main()
