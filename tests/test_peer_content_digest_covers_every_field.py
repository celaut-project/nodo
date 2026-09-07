"""Every semantic field of a `Peer` reaches its content digest (issue #330).

`canonical_peer_content_digest` is built field by field on purpose: protobuf's
`SerializeToString()` is not canonical (field order, unknown fields, non-minimal
varints), so a digest derived from it would differ between two encoders of the same
message. The cost of that choice is that the relationship "the digest covers every
field" is maintained by hand.

Two things ride on it. The digest is what the announcement's signature is taken over,
so a field outside it is a field a relay can rewrite on a claim that still verifies.
And `gateway.utils._sign_peer` keys its cache on the digest and answers a hit with
`peer.CopyFrom(cached)`, so a field outside it is also a field whose fresh value is
discarded and replaced by the cached one for up to the cache TTL.

So the census below is the invariant, not a convention: every field of every message
the digest encodes has to be named as covered or as deliberately excluded, and every
covered field has to come with a mutation proving it moves the digest. Adding a field
to `celaut.proto` fails these until someone decides which it is.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.identity.node_identity import (
        canonical_peer_content_digest,
        canonical_peer_payload,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


# The messages the digest encodes field by field. A field of any of these that nothing
# below accounts for is a hole; a *new message* reached from one of them shows up as a
# new field on its parent, which is caught the same way.
def _censused_messages():
    peer = celaut_pb2.Peer.DESCRIPTOR
    return {
        "Peer": peer,
        "Peer.Uri": celaut_pb2.Peer.Uri.DESCRIPTOR,
        "Peer.Uri.Protocol": celaut_pb2.Peer.Uri.Protocol.DESCRIPTOR,
        "Peer.SignatureScheme": celaut_pb2.Peer.SignatureScheme.DESCRIPTOR,
        "Contract": celaut_pb2.Contract.DESCRIPTOR,
        "Contract.Ledger": celaut_pb2.Contract.Ledger.DESCRIPTOR,
        "ContractRate": celaut_pb2.ContractRate.DESCRIPTOR,
        "Amount": celaut_pb2.Amount.DESCRIPTOR,
    }


# Fields the digest deliberately leaves out, each with the reason. These are not holes:
# they are either the signature machinery itself or covered elsewhere in the signed
# payload (`canonical_peer_payload` signs public_key and ts alongside this digest).
EXCLUDED = {
    "Peer.public_key": "the peer's identity; signed alongside the digest",
    "Peer.signature": "the signature taken over the digest",
    "Peer.ts": "varies per announcement by design; signed alongside the digest",
}


def _uri(peer, ip="1.2.3.4", port=8080):
    uri = peer.uri.add(ip=ip, port=port)
    uri.transport.tags.append("tcp")
    return uri


def _announcement():
    """A Peer with every covered field populated, so a mutation can move any of them."""
    peer = celaut_pb2.Peer()
    uri = _uri(peer)
    uri.expiry_unix_timestamp = 1800000000
    uri.protocol_stack.add(tags=["grpc"], prose="gRPC", formal=b"\x01")
    peer.mu_per_call["start"].n = "1000"
    rate = peer.payment_contracts.add()
    rate.contract.ledger.tags.append("ergo")
    rate.contract.ledger.prose = "Ergo mainnet"
    rate.contract.ledger.formal = b"\x02"
    rate.contract.xattrs["token_id"] = b"\xaa"
    rate.mu_per_unit.n = "1000000000"
    proof = peer.reputation_proofs.add()
    proof.ledger.tags.append("ergo")
    proof.xattrs["token_id"] = b"\xbb"
    peer.signature_scheme.components.add(tags=["ed25519"], prose="Ed25519", formal=b"\x03")
    peer.public_key = "ab" * 32
    peer.signature = "cd" * 64
    peer.ts = 1700000000
    return peer


# One mutation per covered field: the smallest change to that field alone. Keyed by the
# same names the census uses, so a covered field with no mutation is a failure.
MUTATIONS = {
    "Peer.uri": lambda p: _uri(p, ip="5.6.7.8", port=9090),
    "Peer.mu_per_call": lambda p: p.mu_per_call["start"].__setattr__("n", "2000"),
    "Peer.payment_contracts": lambda p: p.payment_contracts[0].contract.xattrs.__setitem__("token_id", b"\xff"),
    "Peer.reputation_proofs": lambda p: p.reputation_proofs[0].xattrs.__setitem__("token_id", b"\xff"),
    "Peer.signature_scheme": lambda p: p.signature_scheme.components.add(tags=["secp256k1"]),
    "Peer.Uri.ip": lambda p: setattr(p.uri[0], "ip", "9.9.9.9"),
    "Peer.Uri.port": lambda p: setattr(p.uri[0], "port", 1234),
    "Peer.Uri.expiry_unix_timestamp": lambda p: setattr(p.uri[0], "expiry_unix_timestamp", 1900000000),
    "Peer.Uri.transport": lambda p: p.uri[0].transport.tags.append("udp"),
    "Peer.Uri.protocol_stack": lambda p: p.uri[0].protocol_stack.add(tags=["http2"]),
    "Peer.Uri.Protocol.tags": lambda p: p.uri[0].protocol_stack[0].tags.append("h2"),
    "Peer.Uri.Protocol.prose": lambda p: setattr(p.uri[0].protocol_stack[0], "prose", "other"),
    "Peer.Uri.Protocol.formal": lambda p: setattr(p.uri[0].protocol_stack[0], "formal", b"\xfe"),
    "Peer.SignatureScheme.components": lambda p: p.signature_scheme.components.add(tags=["schnorr"]),
    "Contract.ledger": lambda p: p.reputation_proofs[0].ledger.tags.append("cardano"),
    "Contract.xattrs": lambda p: p.reputation_proofs[0].xattrs.__setitem__("script", b"\x99"),
    "Contract.Ledger.tags": lambda p: p.payment_contracts[0].contract.ledger.tags.append("mainnet"),
    "Contract.Ledger.prose": lambda p: setattr(p.payment_contracts[0].contract.ledger, "prose", "other"),
    "Contract.Ledger.formal": lambda p: setattr(p.payment_contracts[0].contract.ledger, "formal", b"\xfd"),
    "ContractRate.contract": lambda p: p.payment_contracts[0].contract.ledger.tags.append("testnet"),
    "ContractRate.mu_per_unit": lambda p: setattr(p.payment_contracts[0].mu_per_unit, "n", "5"),
    "Amount.n": lambda p: setattr(p.payment_contracts[0].mu_per_unit, "n", "7"),
}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ContentDigestCoversEveryFieldTests(unittest.TestCase):
    @staticmethod
    def _every_field():
        return {
            f"{name}.{field.name}"
            for name, descriptor in _censused_messages().items()
            for field in descriptor.fields
        }

    def test_every_field_is_accounted_for(self):
        # The census. A field added to celaut.proto lands here until it is either given
        # a mutation (covered) or listed in EXCLUDED with the reason it is not.
        unaccounted = self._every_field() - set(MUTATIONS) - set(EXCLUDED)
        self.assertEqual(
            unaccounted,
            set(),
            "these Peer fields reach neither the digest nor EXCLUDED, so they can be "
            f"rewritten under a valid signature and pinned by the announcement cache: {sorted(unaccounted)}",
        )

    def test_nothing_is_accounted_for_twice(self):
        self.assertEqual(set(MUTATIONS) & set(EXCLUDED), set())

    def test_no_mutation_names_a_field_that_no_longer_exists(self):
        # Keeps the table honest in the other direction: a field renamed or removed in
        # the proto must not leave a mutation behind that quietly tests nothing.
        stale = (set(MUTATIONS) | set(EXCLUDED)) - self._every_field()
        self.assertEqual(stale, set(), f"no such fields any more: {sorted(stale)}")

    def test_every_covered_field_moves_the_digest(self):
        # The census only proves a field was thought about. This proves the digest
        # actually responds to it.
        before = canonical_peer_content_digest(_announcement())
        for field, mutate in sorted(MUTATIONS.items()):
            with self.subTest(field=field):
                peer = _announcement()
                mutate(peer)
                self.assertNotEqual(
                    canonical_peer_content_digest(peer),
                    before,
                    f"changing {field} left the content digest unchanged",
                )

    def test_an_excluded_field_is_signed_by_the_other_half_of_the_payload(self):
        # EXCLUDED claims these are covered elsewhere rather than left unsigned, so
        # check that claim instead of trusting the comment: `canonical_peer_payload`
        # has to respond to both. `Peer.signature` is not here because it is the
        # output, not an input.
        peer = _announcement()
        payload = canonical_peer_payload(
            peer.public_key, peer.ts, canonical_peer_content_digest(peer)
        )
        for field, mutation in (
            ("Peer.public_key", lambda p: setattr(p, "public_key", "ef" * 32)),
            ("Peer.ts", lambda p: setattr(p, "ts", 1800000000)),
        ):
            with self.subTest(field=field):
                other = _announcement()
                mutation(other)
                self.assertNotEqual(
                    canonical_peer_payload(
                        other.public_key, other.ts, canonical_peer_content_digest(other)
                    ),
                    payload,
                    f"{field} is excluded from the digest and unsigned everywhere else",
                )

    def test_an_excluded_field_does_not_move_the_digest(self):
        # The other half: the cache must hit for a re-signed announcement of identical
        # content, which is what it exists for.
        before = canonical_peer_content_digest(_announcement())
        for field, mutation in (
            ("Peer.public_key", lambda p: setattr(p, "public_key", "ef" * 32)),
            ("Peer.signature", lambda p: setattr(p, "signature", "12" * 64)),
            ("Peer.ts", lambda p: setattr(p, "ts", 1800000000)),
        ):
            with self.subTest(field=field):
                peer = _announcement()
                mutation(peer)
                self.assertEqual(canonical_peer_content_digest(peer), before)


if __name__ == "__main__":
    unittest.main()
