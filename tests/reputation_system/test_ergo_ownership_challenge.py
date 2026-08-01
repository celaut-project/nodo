"""R7 ownership challenge: signature verify, sign_message identity fix, gRPC challenge (#186 4.1)."""
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
from unittest import mock

from bip32 import BIP32, HARDENED_INDEX
from mnemonic import Mnemonic

from src.reputation_system.bip_wallet_verification import (
    bip_ecdsa_sign,
    bip_ecdsa_verify_proposition,
    public_key_from_proposition_bytes,
)
from src.reputation_system.contracts.ergo import proof_validation


def _proposition_for(mnemonic: str) -> bytes:
    seed = Mnemonic("english").to_seed(mnemonic, passphrase="")
    b = BIP32.from_seed(seed)
    pk = b.get_pubkey_from_path([44 + HARDENED_INDEX, 429 + HARDENED_INDEX, 0 + HARDENED_INDEX, 0, 0])
    return bytes.fromhex("0008cd") + pk


MNEMONIC = Mnemonic("english").generate(strength=128)
OTHER = Mnemonic("english").generate(strength=128)
PROP = _proposition_for(MNEMONIC)


class OwnershipChallengeCryptoTests(unittest.TestCase):
    def test_verify_accepts_owner_signature(self):
        sig = bip_ecdsa_sign(MNEMONIC, "challenge-123")
        self.assertTrue(bip_ecdsa_verify_proposition(PROP, "challenge-123", sig))

    def test_verify_rejects_wrong_message_key_and_garbage(self):
        sig = bip_ecdsa_sign(MNEMONIC, "challenge-123")
        self.assertFalse(bip_ecdsa_verify_proposition(PROP, "other", sig))
        self.assertFalse(bip_ecdsa_verify_proposition(_proposition_for(OTHER), "challenge-123", sig))
        self.assertFalse(bip_ecdsa_verify_proposition(b"\x00", "challenge-123", sig))
        self.assertFalse(bip_ecdsa_verify_proposition(PROP, "challenge-123", "zz"))

    def test_public_key_extraction(self):
        self.assertEqual(public_key_from_proposition_bytes(PROP), PROP[3:])
        with self.assertRaises(ValueError):
            public_key_from_proposition_bytes(b"\x10" + b"\x00" * 33)


class SignMessageTests(unittest.TestCase):
    """sign_message must compare by value (== not `is`) and sign only exact matches."""

    def _patch_wallet(self, mnemonic):
        cfg = mock.patch.object(
            proof_validation, "ConfigManager",
            return_value=mock.Mock(get=lambda k, *a: mnemonic if k == "ledgers.ergo.WALLET_MNEMONIC" else None),
        )
        gpk = mock.patch.object(proof_validation, "get_public_key", lambda mnemonic_phrase: mnemonic_phrase)
        opb = mock.patch.object(proof_validation, "owner_proposition_bytes", _proposition_for)
        return cfg, gpk, opb

    def test_signs_only_when_proposition_matches_local_wallet(self):
        cfg, gpk, opb = self._patch_wallet(MNEMONIC)
        with cfg, gpk, opb:
            sig = proof_validation.sign_message(proposition_bytes=PROP, message="chal")
            self.assertIsNotNone(sig)
            self.assertTrue(bip_ecdsa_verify_proposition(PROP, "chal", sig))

    def test_refuses_foreign_proposition(self):
        cfg, gpk, opb = self._patch_wallet(MNEMONIC)
        with cfg, gpk, opb:
            # Challenge for someone else's propositionBytes -> must NOT sign.
            self.assertIsNone(proof_validation.sign_message(proposition_bytes=_proposition_for(OTHER), message="chal"))

    def test_accepts_hex_and_bytes_challenge(self):
        cfg, gpk, opb = self._patch_wallet(MNEMONIC)
        with cfg, gpk, opb:
            self.assertIsNotNone(proof_validation.sign_message(proposition_bytes=PROP.hex(), message=b"chal"))

    def test_missing_mnemonic_returns_none(self):
        cfg, gpk, opb = self._patch_wallet("")
        with cfg, gpk, opb:
            self.assertIsNone(proof_validation.sign_message(proposition_bytes=PROP, message="chal"))


class ChallengePeerOwnershipTests(unittest.TestCase):
    """_challenge_peer_ownership over a mocked Gateway.SignPublicKey."""

    def test_challenge_succeeds_when_peer_signs(self):
        captured = {}

        def fake_client_grpc(method, input, indices_parser, partitions_message_mode_parser, timeout):
            from protos import celaut_pb2
            captured["challenge"] = input.to_sign
            captured["prop"] = bytes.fromhex(input.public_key)
            sig = bip_ecdsa_sign(MNEMONIC, captured["challenge"])
            yield celaut_pb2.SignResponse(signed=sig)

        with mock.patch("src.utils.utils.generate_uris_by_peer_id", return_value=iter(["1.2.3.4:5"])), \
             mock.patch("grpc.insecure_channel"), \
             mock.patch("src.reputation_system.contracts.ergo.proof_validation.celaut_pb2_grpc.GatewayStub"), \
             mock.patch.object(proof_validation.bee, "client_grpc", side_effect=fake_client_grpc):
            ok = proof_validation._challenge_peer_ownership("peer-1", PROP.hex())
        self.assertTrue(ok)
        self.assertEqual(captured["prop"], PROP)  # raw proposition bytes sent, not text

    def test_challenge_fails_on_bad_signature(self):
        def fake_client_grpc(method, input, indices_parser, partitions_message_mode_parser, timeout):
            from protos import celaut_pb2
            sig = bip_ecdsa_sign(OTHER, input.to_sign)  # wrong key
            yield celaut_pb2.SignResponse(signed=sig)

        with mock.patch("src.utils.utils.generate_uris_by_peer_id", return_value=iter(["1.2.3.4:5"])), \
             mock.patch("grpc.insecure_channel"), \
             mock.patch("src.reputation_system.contracts.ergo.proof_validation.celaut_pb2_grpc.GatewayStub"), \
             mock.patch.object(proof_validation.bee, "client_grpc", side_effect=fake_client_grpc):
            self.assertFalse(proof_validation._challenge_peer_ownership("peer-1", PROP.hex()))

    def test_challenge_fails_when_no_uri(self):
        with mock.patch("src.utils.utils.generate_uris_by_peer_id", return_value=iter([])):
            self.assertFalse(proof_validation._challenge_peer_ownership("peer-1", PROP.hex()))

    def test_challenge_fails_on_empty_signature(self):
        def fake_client_grpc(method, input, indices_parser, partitions_message_mode_parser, timeout):
            from protos import celaut_pb2
            yield celaut_pb2.SignResponse(signed="")

        with mock.patch("src.utils.utils.generate_uris_by_peer_id", return_value=iter(["1.2.3.4:5"])), \
             mock.patch("grpc.insecure_channel"), \
             mock.patch("src.reputation_system.contracts.ergo.proof_validation.celaut_pb2_grpc.GatewayStub"), \
             mock.patch.object(proof_validation.bee, "client_grpc", side_effect=fake_client_grpc):
            self.assertFalse(proof_validation._challenge_peer_ownership("peer-1", PROP.hex()))


if __name__ == "__main__":
    unittest.main()
