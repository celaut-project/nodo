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


def _instance(ip="1.2.3.4", port=80, rate="10", contract=b"HONEST"):
    instance = celaut_pb2.Instance()
    slot = instance.uri_slot.add()
    slot.internal_port = port
    slot.uri.add(ip=ip, port=port)
    api_slot = instance.api.slot.add()
    api_slot.port = port
    api_slot.transport.tags.append("tcp")
    api_slot.gas_amount_per_call["exec"].n = rate
    gas_price = instance.api.payment_contracts.add()
    gas_price.contract.ledger.formal = contract
    gas_price.gas_amount.n = "1"
    return instance


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


class CanonicalInstanceDigestTests(unittest.TestCase):
    def test_is_stable_and_order_independent(self):
        a, b = _instance(), _instance()
        b.uri_slot[0].uri.add(ip="9.9.9.9", port=80)
        a.uri_slot[0].uri.add(ip="9.9.9.9", port=80)
        self.assertEqual(ni.canonical_instance_digest(a), ni.canonical_instance_digest(b))

    def test_covers_the_payment_contract(self):
        # The hole this closes: a relayed Peer whose contract was swapped must not
        # keep verifying, or an attacker redirects this node's payments.
        base = ni.canonical_instance_digest(_instance(contract=b"HONEST"))
        self.assertNotEqual(base, ni.canonical_instance_digest(_instance(contract=b"ATTACKER")))

    def test_covers_the_advertised_rates_and_addresses(self):
        base = ni.canonical_instance_digest(_instance())
        self.assertNotEqual(base, ni.canonical_instance_digest(_instance(rate="999999")))
        self.assertNotEqual(base, ni.canonical_instance_digest(_instance(ip="6.6.6.6")))
        self.assertNotEqual(base, ni.canonical_instance_digest(_instance(port=81)))


class CanonicalPayloadTests(unittest.TestCase):
    def test_differs_on_any_field(self):
        digest = ni.canonical_instance_digest(_instance())
        base = ni.canonical_peer_payload("ab", 1, 2, digest)
        self.assertNotEqual(base, ni.canonical_peer_payload("cd", 1, 2, digest))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 9, 2, digest))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 1, 9, digest))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 1, 2, "deadbeef"))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 1, 2, digest, 999))


class SignAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self.pubkey_hex = derive_compressed_pubkey(MNEMONIC).hex()
        self.digest = ni.canonical_instance_digest(_instance())
        self.payload = ni.canonical_peer_payload(self.pubkey_hex, 100, 1, self.digest)
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
        signed = ni.canonical_peer_payload(self.pubkey_hex, 100, 1, self.digest, 0)
        tampered = ni.canonical_peer_payload(self.pubkey_hex, 100, 1, self.digest, 2 ** 40)
        signature = bip_ecdsa_sign(MNEMONIC, signed)
        self.assertTrue(ni.verify_peer_payload(self.pubkey_hex, signed, signature))
        self.assertFalse(ni.verify_peer_payload(self.pubkey_hex, tampered, signature))


if __name__ == "__main__":
    unittest.main()
