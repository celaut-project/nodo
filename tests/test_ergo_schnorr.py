"""Ergo Schnorr signatures: interoperability with the reference implementations.

The vectors below are the cross-validation vectors published in basis-tracker
(``specs/SCHNORR_SIGNATURE_SPEC.md``), generated from the Scala reference
(``basis/offchain/SigUtils.scala``). They are the only external oracle available for this
primitive, and they are what keeps ``src/utils/ergo_schnorr.py`` from drifting into a
nodo-only variant of the scheme: a signature produced elsewhere in the Ergo ecosystem must
verify here, byte for byte, with no re-encoding.

The message they sign is Basis's IOU note message (``key || totalDebt || timestamp``); the
signature *scheme* is the same one node identity uses, which is the point of reusing them.
"""
import unittest

from src.utils.ergo_schnorr import ORDER, blake2b256, challenge, sign, verify

# basis-tracker specs/SCHNORR_SIGNATURE_SPEC.md, "Cross-Validation Test Vectors".
ISSUER = "0284bf7562262bbd6940085748f3be6afa52ae317155181ece31b66351ccffa4b0"
RECIPIENT = "02207bba70bc66309baa582a6ac120fd52d68026c51f6326f8ccedcbd2c1b7eb82"
TRACKER = "037c3f0429768437a942f1818ef1616c609b7a6d8a8dd245e179c8c0838e7d169d"

TV001_MESSAGE = "07b67390866bedf6c19b3fab1e29993ea6878e0d0dd0577ac6b6368c96a1220b000000003b9aca0000000195e97f7800"
TV001_SIGNATURE = "0389ec7df5ff00fcdf83f41ad41ef1813cfd64a87b6c7f219bcd1ecfae9b82a1041af95c9171d4ad63e29513701cdeb5cc9f45798276947c8a8b361dae0f94ab93"
# TV003: the same (owner, receiver) pair, signed by the tracker rather than the issuer.
TV003_MESSAGE = "07b67390866bedf6c19b3fab1e29993ea6878e0d0dd0577ac6b6368c96a1220b000000001dcd650000000195e97f7be8"
TV003_SIGNATURE = "024900b6f2a6c83c9158420e7e15bc211e761f5157fe84f2a25499340e731c420624c6b3f14a59b811d50ab0492e53784b541a53688452898924142a313cb64a37"
# TV004: a valid signature by a *different* key over TV001's message.
TV004_SIGNATURE = "03896bab104009190272b8f99808d3d04654f3a882c04aa4119fdffe352e7d496e31f2cc1a52fb60cd3ea7eb5919929584b83f4e9fd7122ea28c9a5ff20090e782"
# TV005: TV001's message with a corrupted signature.
TV005_SIGNATURE = "0224f5a465dc99fe66177dbb503363bcd12a679b260783adc2305dfa996feb5e9564afadb695cf16d8ff1500f557bc0fff7cfb28e418bac449748a09a5ffb7dce3"
# TV006: TV001's signature against a message whose amount was altered by one.
TV006_MESSAGE = "07b67390866bedf6c19b3fab1e29993ea6878e0d0dd0577ac6b6368c96a1220b000000003b9aca0100000195e97f7800"
TV006_SIGNATURE = "028fd39a0481ab31003d979a8276655c020530038ee18046a441296c4f4b8bbebf38fdbd14ac7fedbfef993d02ef3941dd9fb1f3f287e7bf56a93bf0dd6af67456"


def note_message(owner: str, receiver: str, total_debt: int, timestamp: int) -> bytes:
    """Basis's 48-byte note message: ``blake2b256(owner||receiver) || debt || timestamp``."""
    return (
        blake2b256(bytes.fromhex(owner) + bytes.fromhex(receiver))
        + total_debt.to_bytes(8, "big")
        + timestamp.to_bytes(8, "big")
    )


def _verify(message_hex: str, public_key_hex: str, signature_hex: str) -> bool:
    return verify(
        bytes.fromhex(message_hex), bytes.fromhex(public_key_hex), bytes.fromhex(signature_hex)
    )


class ReferenceVectorTests(unittest.TestCase):
    def test_message_construction_matches_the_reference(self):
        # Reproducing the documented message byte for byte is what proves our hash,
        # concatenation order and big-endian widths agree with the reference.
        self.assertEqual(
            note_message(ISSUER, RECIPIENT, 1_000_000_000, 1_743_379_200_000).hex(),
            TV001_MESSAGE,
        )

    def test_accepts_reference_signature(self):
        self.assertTrue(_verify(TV001_MESSAGE, ISSUER, TV001_SIGNATURE))

    def test_rejects_all_zero_signature(self):
        self.assertFalse(_verify(TV001_MESSAGE, ISSUER, "00" * 65))

    def test_rejects_signature_from_another_key(self):
        self.assertFalse(_verify(TV001_MESSAGE, ISSUER, TV004_SIGNATURE))

    def test_rejects_corrupted_signature(self):
        self.assertFalse(_verify(TV001_MESSAGE, ISSUER, TV005_SIGNATURE))

    def test_rejects_altered_amount(self):
        self.assertFalse(_verify(TV006_MESSAGE, ISSUER, TV006_SIGNATURE))

    def test_signature_is_verified_against_the_signer_not_the_issuer(self):
        # The signer's public key is part of the challenge, so a tracker signature over a
        # note verifies against the tracker's key -- never against the note's issuer.
        self.assertFalse(_verify(TV003_MESSAGE, ISSUER, TV003_SIGNATURE))

    def test_negative_challenge_signature_is_rejected_as_on_chain(self):
        """TV003 carries a negative challenge, and is rejected -- deliberately.

        Its ``e`` has the top bit set, so ErgoScript's ``byteArrayToBigInt`` reads it as a
        negative integer and the reserve contract's ``g.exp(z) == a.multiply(pk.exp(e))``
        does not hold. basis-tracker's Rust verifier reads ``e`` as unsigned and accepts
        it; we follow the contract, because a note we accept off-chain and cannot redeem
        on-chain is a loss, not a payment. Reported upstream.
        """
        a_bytes = bytes.fromhex(TV003_SIGNATURE)[:33]
        e = challenge(a_bytes, bytes.fromhex(TV003_MESSAGE), bytes.fromhex(TRACKER))
        self.assertLess(e, 0, "vector no longer exercises the negative-challenge case")
        self.assertFalse(_verify(TV003_MESSAGE, TRACKER, TV003_SIGNATURE))


class SignAndVerifyTests(unittest.TestCase):
    SECRET = 0x1F2E3D4C5B6A79889796A5B4C3D2E1F00F1E2D3C4B5A69788897A6B5C4D3E2F1

    def setUp(self):
        import ecdsa

        self.signing_key = ecdsa.SigningKey.from_secret_exponent(
            self.SECRET, curve=ecdsa.SECP256k1
        )
        self.public_key = self.signing_key.get_verifying_key().to_string("compressed")

    def _sign(self, message: bytes) -> bytes:
        return sign(message, self.SECRET, self.public_key)

    def test_roundtrip(self):
        message = b"peer-payload|1700000000|deadbeef"
        self.assertTrue(verify(message, self.public_key, self._sign(message)))

    def test_rejects_other_message_and_other_key(self):
        signature = self._sign(b"one")
        self.assertFalse(verify(b"two", self.public_key, signature))
        other = b"\x02" + b"\x11" * 32  # not a curve point
        self.assertFalse(verify(b"one", other, signature))

    def test_signature_shape_is_on_chain_safe(self):
        """65 bytes, a valid compressed point, and both scalars positive when read signed.

        Every signature must satisfy the constraints the on-chain verifier imposes,
        whatever nonce was drawn -- so this asserts over several signatures rather than
        one. ``z`` with its top bit set would be read as negative by
        ``byteArrayToBigInt``; a negative ``e`` would make the signature unverifiable
        under the signed convention the contract uses.
        """
        message = b"shape"
        for _ in range(12):
            signature = self._sign(message)
            self.assertEqual(len(signature), 65)
            a_bytes, z_bytes = signature[:33], signature[33:]
            self.assertIn(a_bytes[0], (0x02, 0x03))
            self.assertLess(z_bytes[0], 0x80, "z would be negative on-chain")
            self.assertEqual(len(z_bytes), 32)
            self.assertTrue(0 < int.from_bytes(z_bytes, "big") < ORDER)
            self.assertGreater(challenge(a_bytes, message, self.public_key), 0)
            self.assertTrue(verify(message, self.public_key, signature))

    def test_nonce_is_not_reused(self):
        # Two signatures over the same message must differ: a repeated `a` would mean a
        # repeated nonce, which leaks the private key.
        first, second = self._sign(b"same"), self._sign(b"same")
        self.assertNotEqual(first[:33], second[:33])

    def test_rejects_malformed_inputs(self):
        signature = self._sign(b"m")
        self.assertFalse(verify(b"m", self.public_key, signature[:-1]))
        self.assertFalse(verify(b"m", self.public_key[:-1], signature))
        self.assertFalse(verify(b"m", self.public_key, b"\x04" + signature[1:]))
        # z = 0 and z = ORDER are out of range.
        self.assertFalse(verify(b"m", self.public_key, signature[:33] + b"\x00" * 32))
        self.assertFalse(
            verify(b"m", self.public_key, signature[:33] + ORDER.to_bytes(32, "big"))
        )

    def test_rejects_private_key_out_of_range(self):
        with self.assertRaises(ValueError):
            sign(b"m", 0, self.public_key)
        with self.assertRaises(ValueError):
            sign(b"m", ORDER, self.public_key)


if __name__ == "__main__":
    unittest.main()
