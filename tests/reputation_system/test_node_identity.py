"""Node identity keypair, the canonical signed payload, and what it covers (#236)."""
import sys as _sys
_stub = _sys.modules.get("mnemonic")
if _stub is not None and not hasattr(getattr(_stub, "Mnemonic", None), "to_seed"):
    del _sys.modules["mnemonic"]
    for _n in [n for n in list(_sys.modules) if n.startswith("src.reputation_system.")]:
        del _sys.modules[_n]
import unittest

from mnemonic import Mnemonic

from protos import celaut_pb2
from src.reputation_system import node_identity as ni
from src.reputation_system.bip_wallet_verification import bip_ecdsa_sign, derive_compressed_pubkey

MNEMONIC = Mnemonic("english").generate(strength=128)
OTHER_MNEMONIC = Mnemonic("english").generate(strength=128)


def _peer(ip="1.2.3.4", port=80, rate="10", contract=b"HONEST", expiry=0, transport="tcp"):
    peer = celaut_pb2.Peer()
    uri = peer.uri.add(ip=ip, port=port, expiry_unix_timestamp=expiry)
    uri.transport.tags.append(transport)
    uri.protocol_stack.add(tags=["grpc"])
    peer.mu_per_call["exec"].n = rate
    gas_price = peer.payment_contracts.add()
    gas_price.contract.ledger.formal = contract
    gas_price.mu_per_unit.n = "1"
    return peer


class NormalizePublicKeyTests(unittest.TestCase):
    def test_accepts_canonical_lowercase_hex(self):
        key = "02" + "ab" * 32
        self.assertEqual(ni.normalize_public_key_hex(key), key)

    def test_lowercases_and_strips(self):
        key = "02" + "AB" * 32
        self.assertEqual(ni.normalize_public_key_hex("  " + key + " "), key.lower())

    def test_rejects_wrong_length_and_non_hex(self):
        # bytes.fromhex would accept the embedded space; a peer_id must not.
        self.assertIsNone(ni.normalize_public_key_hex("02 " + "ab" * 32))
        self.assertIsNone(ni.normalize_public_key_hex("02ab"))
        self.assertIsNone(ni.normalize_public_key_hex("zz" * 33))
        self.assertIsNone(ni.normalize_public_key_hex(""))


class CanonicalPeerContentDigestTests(unittest.TestCase):
    def test_is_stable_and_order_independent(self):
        a, b = _peer(), _peer()
        b.uri.add(ip="9.9.9.9", port=80)
        a.uri.add(ip="9.9.9.9", port=80)
        self.assertEqual(
            ni.canonical_peer_content_digest(a), ni.canonical_peer_content_digest(b)
        )

    def test_covers_the_payment_contract(self):
        # The hole this closes: a relayed Peer whose contract was swapped must not
        # keep verifying, or an attacker redirects this node's payments.
        base = ni.canonical_peer_content_digest(_peer(contract=b"HONEST"))
        self.assertNotEqual(base, ni.canonical_peer_content_digest(_peer(contract=b"ATTACKER")))

    def test_covers_the_advertised_rates_and_addresses(self):
        base = ni.canonical_peer_content_digest(_peer())
        for other in (_peer(rate="999999"), _peer(ip="6.6.6.6"), _peer(port=81)):
            self.assertNotEqual(base, ni.canonical_peer_content_digest(other))

    def test_covers_each_uri_own_expiry(self):
        # Expiry lives on each Peer.Uri; it must still be covered by the digest, so
        # it cannot be stripped or extended in transit.
        self.assertNotEqual(
            ni.canonical_peer_content_digest(_peer(expiry=0)),
            ni.canonical_peer_content_digest(_peer(expiry=999_999)),
        )

    def test_covers_the_declared_transport(self):
        # Downgrading a UDP endpoint to TCP (or the reverse) would send a reader at
        # the address with the wrong kind of socket, so it must break the signature.
        self.assertNotEqual(
            ni.canonical_peer_content_digest(_peer(transport="tcp")),
            ni.canonical_peer_content_digest(_peer(transport="udp")),
        )

    def test_covers_the_reputation_proofs(self):
        # The signed message is republished on-chain as "verifiable against the
        # peer's key", so a relay must not be able to graft proofs onto it -- nor
        # strip the legitimate ones -- while the signature still checks out.
        grafted = _peer()
        grafted.reputation_proofs.add().ledger.formal = b"GRAFTED"
        self.assertNotEqual(
            ni.canonical_peer_content_digest(_peer()),
            ni.canonical_peer_content_digest(grafted),
        )

    def test_covers_the_protocol_stack(self):
        swapped = _peer()
        del swapped.uri[0].protocol_stack[:]
        swapped.uri[0].protocol_stack.add(tags=["attacker"])
        self.assertNotEqual(
            ni.canonical_peer_content_digest(_peer()),
            ni.canonical_peer_content_digest(swapped),
        )


class CanonicalPayloadTests(unittest.TestCase):
    def test_differs_on_any_field(self):
        digest = ni.canonical_peer_content_digest(_peer())
        base = ni.canonical_peer_payload("ab", 1, digest)
        self.assertNotEqual(base, ni.canonical_peer_payload("cd", 1, digest))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 9, digest))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 1, "deadbeef"))


class SignAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self.pubkey_hex = derive_compressed_pubkey(MNEMONIC).hex()
        self.digest = ni.canonical_peer_content_digest(_peer())
        self.payload = ni.canonical_peer_payload(self.pubkey_hex, 100, self.digest)
        self.signature = bip_ecdsa_sign(MNEMONIC, self.payload)

    def test_valid_signature_verifies(self):
        self.assertTrue(ni.verify_peer_payload(self.pubkey_hex, self.payload, self.signature))

    def test_tampered_payload_fails(self):
        self.assertFalse(ni.verify_peer_payload(self.pubkey_hex, self.payload + "x", self.signature))

    def test_wrong_public_key_fails(self):
        other = derive_compressed_pubkey(OTHER_MNEMONIC).hex()
        self.assertFalse(ni.verify_peer_payload(other, self.payload, self.signature))

    def test_non_canonical_public_key_is_refused(self):
        self.assertFalse(
            ni.verify_peer_payload(self.pubkey_hex.upper(), self.payload, self.signature)
        )

    def test_malformed_or_empty_inputs_fail_closed(self):
        self.assertFalse(ni.verify_peer_payload("not-hex", self.payload, self.signature))
        self.assertFalse(ni.verify_peer_payload("", self.payload, self.signature))
        self.assertFalse(ni.verify_peer_payload(self.pubkey_hex, self.payload, ""))

    def test_node_proposition_hex_is_r7_shaped(self):
        self.assertEqual(ni.node_proposition_hex(self.pubkey_hex), "0008cd" + self.pubkey_hex)

    def test_tampering_with_the_expiry_estimate_breaks_the_signature(self):
        # expiry_unix_timestamp lives on each Peer.Uri, folded into the content
        # digest -- stretching it must still invalidate the signature.
        signed = ni.canonical_peer_payload(
            self.pubkey_hex, 100, ni.canonical_peer_content_digest(_peer(expiry=0))
        )
        tampered = ni.canonical_peer_payload(
            self.pubkey_hex, 100, ni.canonical_peer_content_digest(_peer(expiry=2 ** 40))
        )
        signature = bip_ecdsa_sign(MNEMONIC, signed)
        self.assertTrue(ni.verify_peer_payload(self.pubkey_hex, signed, signature))
        self.assertFalse(ni.verify_peer_payload(self.pubkey_hex, tampered, signature))


if __name__ == "__main__":
    unittest.main()
