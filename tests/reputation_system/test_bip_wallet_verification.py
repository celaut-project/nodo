"""BIP32 derivation and Ergo Schnorr signing shared by node identity (#236) and the proof."""
# Some sibling test modules install a stub `mnemonic` in sys.modules (to prove lazy
# imports). Restore the real package here so this module's real BIP39/BIP32 crypto works
# regardless of collection order.
import sys as _sys
_stub = _sys.modules.get("mnemonic")
if _stub is not None and not hasattr(getattr(_stub, "Mnemonic", None), "to_seed"):
    del _sys.modules["mnemonic"]
    for _n in [n for n in list(_sys.modules) if n.startswith("src.reputation_system.bip_wallet_verification")]:
        del _sys.modules[_n]
import unittest

from bip32 import BIP32, HARDENED_INDEX
from mnemonic import Mnemonic

from src.reputation_system.bip_wallet_verification import (
    bip_schnorr_sign,
    bip_schnorr_verify_proposition,
    derive_compressed_pubkey,
    public_key_from_proposition_bytes,
)


def _proposition_for(mnemonic: str) -> bytes:
    seed = Mnemonic("english").to_seed(mnemonic, passphrase="")
    b = BIP32.from_seed(seed)
    pk = b.get_pubkey_from_path([44 + HARDENED_INDEX, 429 + HARDENED_INDEX, 0 + HARDENED_INDEX, 0, 0])
    return bytes.fromhex("0008cd") + pk


MNEMONIC = Mnemonic("english").generate(strength=128)
OTHER = Mnemonic("english").generate(strength=128)
PROP = _proposition_for(MNEMONIC)


class OwnershipVerificationCryptoTests(unittest.TestCase):
    def test_verify_accepts_owner_signature(self):
        sig = bip_schnorr_sign(MNEMONIC, "challenge-123")
        self.assertTrue(bip_schnorr_verify_proposition(PROP, "challenge-123", sig))

    def test_verify_rejects_wrong_message_key_and_garbage(self):
        sig = bip_schnorr_sign(MNEMONIC, "challenge-123")
        self.assertFalse(bip_schnorr_verify_proposition(PROP, "other", sig))
        self.assertFalse(bip_schnorr_verify_proposition(_proposition_for(OTHER), "challenge-123", sig))
        self.assertFalse(bip_schnorr_verify_proposition(b"\x00", "challenge-123", sig))
        self.assertFalse(bip_schnorr_verify_proposition(PROP, "challenge-123", "zz"))

    def test_public_key_extraction(self):
        self.assertEqual(public_key_from_proposition_bytes(PROP), PROP[3:])
        with self.assertRaises(ValueError):
            public_key_from_proposition_bytes(b"\x10" + b"\x00" * 33)


class DeriveCompressedPubkeyTests(unittest.TestCase):
    def test_matches_the_ergo_derivation_path(self):
        # Same derivation as sign_message/bip_schnorr_sign's proposition bytes,
        # computed independently in pure Python (no JVM) -- see node_identity.py.
        self.assertEqual(bytes.fromhex("0008cd") + derive_compressed_pubkey(MNEMONIC), PROP)

    def test_rejects_invalid_mnemonic(self):
        with self.assertRaises(ValueError):
            derive_compressed_pubkey("not a valid mnemonic phrase at all")


if __name__ == "__main__":
    unittest.main()
