"""Node identity keypair, canonical signed payload, and its (ts, seq) semantics (#236)."""
import sys as _sys
_stub = _sys.modules.get("mnemonic")
if _stub is not None and not hasattr(getattr(_stub, "Mnemonic", None), "to_seed"):
    del _sys.modules["mnemonic"]
    for _n in [n for n in list(_sys.modules) if n.startswith("src.reputation_system.")]:
        del _sys.modules[_n]
import unittest

from mnemonic import Mnemonic

from src.reputation_system import node_identity as ni
from src.reputation_system.bip_wallet_verification import bip_ecdsa_sign, derive_compressed_pubkey

MNEMONIC = Mnemonic("english").generate(strength=128)
OTHER_MNEMONIC = Mnemonic("english").generate(strength=128)


class CanonicalPayloadTests(unittest.TestCase):
    def test_sorts_uris_so_order_does_not_affect_the_signature(self):
        a = ni.canonical_peer_payload("ab", 1, 2, ["2.2.2.2:2", "1.1.1.1:1"])
        b = ni.canonical_peer_payload("ab", 1, 2, ["1.1.1.1:1", "2.2.2.2:2"])
        self.assertEqual(a, b)

    def test_differs_on_any_field(self):
        base = ni.canonical_peer_payload("ab", 1, 2, ["1.1.1.1:1"])
        self.assertNotEqual(base, ni.canonical_peer_payload("cd", 1, 2, ["1.1.1.1:1"]))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 9, 2, ["1.1.1.1:1"]))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 1, 9, ["1.1.1.1:1"]))
        self.assertNotEqual(base, ni.canonical_peer_payload("ab", 1, 2, ["1.1.1.1:9"]))


class SignAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self.pubkey_hex = derive_compressed_pubkey(MNEMONIC).hex()
        self.payload = ni.canonical_peer_payload(self.pubkey_hex, 100, 1, ["1.2.3.4:80"])
        self.signature = bip_ecdsa_sign(MNEMONIC, self.payload)

    def test_valid_signature_verifies(self):
        self.assertTrue(ni.verify_peer_payload(self.pubkey_hex, self.payload, self.signature))

    def test_tampered_payload_fails(self):
        self.assertFalse(ni.verify_peer_payload(self.pubkey_hex, self.payload + "x", self.signature))

    def test_wrong_public_key_fails(self):
        other_pubkey_hex = derive_compressed_pubkey(OTHER_MNEMONIC).hex()
        self.assertFalse(ni.verify_peer_payload(other_pubkey_hex, self.payload, self.signature))

    def test_malformed_public_key_fails_closed(self):
        self.assertFalse(ni.verify_peer_payload("not-hex", self.payload, self.signature))

    def test_empty_public_key_or_signature_fails_closed(self):
        self.assertFalse(ni.verify_peer_payload("", self.payload, self.signature))
        self.assertFalse(ni.verify_peer_payload(self.pubkey_hex, self.payload, ""))

    def test_node_proposition_hex_is_r7_shaped(self):
        self.assertEqual(ni.node_proposition_hex(self.pubkey_hex), "0008cd" + self.pubkey_hex)


if __name__ == "__main__":
    unittest.main()
