"""A wallet vouching for the node that announced its proof (issue #236).

The bridge between an identity on no ledger and an owner Ergo names in R7: these pin
that the claim is checkable, that it cannot be lifted onto another node, and that two
proofs of one peer may have been published by different wallets.
"""
import sys as _sys
_stub = _sys.modules.get("mnemonic")
if _stub is not None and not hasattr(getattr(_stub, "Mnemonic", None), "to_seed"):
    del _sys.modules["mnemonic"]
    for _n in [n for n in list(_sys.modules) if n.startswith("src.reputation_system.")]:
        del _sys.modules[_n]
import unittest
import unittest.mock

from mnemonic import Mnemonic

from protos import celaut_pb2
from src.reputation_system import proof_attestation as pa
from src.reputation_system.bip_wallet_verification import (
    bip_schnorr_sign,
    derive_compressed_pubkey,
)
from src.utils import node_identity as ni
from src.utils.contract_xattrs import set_owner_attestation

MNEMONIC = Mnemonic("english").generate(strength=128)
OTHER_MNEMONIC = Mnemonic("english").generate(strength=128)


def _identity(mnemonic):
    """``(public key hex, signer)`` for a mnemonic, the way the node derives its own."""
    public_key_hex, private_key = ni._cached_keypair(mnemonic)
    return public_key_hex, lambda payload: private_key.sign(payload.encode("utf-8")).hex()


class PropositionBytesTests(unittest.TestCase):
    def test_node_proposition_hex_is_r7_shaped(self):
        # R7 holds a *wallet's* propositionBytes, so this is fed a wallet key.
        wallet = derive_compressed_pubkey(MNEMONIC).hex()
        self.assertEqual(pa.node_proposition_hex(wallet), "0008cd" + wallet)


class ProofOwnerAttestationTests(unittest.TestCase):
    """A wallet vouching for an identity, which is what ties a proof to a node."""

    def setUp(self):
        self.peer_id, _ = _identity(MNEMONIC)
        self.wallet = derive_compressed_pubkey(MNEMONIC).hex()

    def _proof(self, wallet=None, signed_over=None, mnemonic=MNEMONIC):
        contract = celaut_pb2.Contract()
        contract.ledger.tags.append("ergo")
        set_owner_attestation(
            contract,
            wallet or self.wallet,
            bip_schnorr_sign(
                mnemonic, ni.attestation_payload(signed_over or self.peer_id)
            ),
        )
        return contract

    def test_a_wallet_that_signed_this_peer_id_is_attested(self):
        self.assertEqual(
            pa.attested_proof_owner(self._proof(), self.peer_id), self.wallet
        )

    def test_an_attestation_for_another_peer_id_is_refused(self):
        # The attack this stops: lifting a real attestation off one node's proof and
        # pasting it onto another's to inherit its reputation.
        other_peer_id, _ = _identity(OTHER_MNEMONIC)
        self.assertIsNone(pa.attested_proof_owner(self._proof(), other_peer_id))

    def test_a_wallet_that_did_not_sign_is_refused(self):
        # Naming someone else's wallet is free; signing with it is not.
        other_wallet = derive_compressed_pubkey(OTHER_MNEMONIC).hex()
        self.assertIsNone(
            pa.attested_proof_owner(self._proof(wallet=other_wallet), self.peer_id)
        )

    def test_a_proof_with_no_attestation_at_all(self):
        contract = celaut_pb2.Contract()
        contract.ledger.tags.append("ergo")
        self.assertIsNone(pa.attested_proof_owner(contract, self.peer_id))

    def test_two_proofs_may_have_different_owners(self):
        # What an attestation per peer could not express: a node holds as many proofs as
        # it likes (issue #281) and nothing says they share a wallet.
        other_wallet = derive_compressed_pubkey(OTHER_MNEMONIC).hex()
        second = self._proof(wallet=other_wallet, mnemonic=OTHER_MNEMONIC)

        self.assertEqual(
            pa.attested_proof_owner(self._proof(), self.peer_id), self.wallet
        )
        self.assertEqual(
            pa.attested_proof_owner(second, self.peer_id), other_wallet
        )

    def test_attesting_writes_a_verifiable_pair(self):
        # The round trip: what this node signs is what a reader accepts.
        contract = celaut_pb2.Contract()
        with unittest.mock.patch.object(
            pa, "get_node_public_key_hex", lambda: self.peer_id
        ):
            self.assertTrue(pa.attest_proof_ownership(contract, MNEMONIC))
        self.assertEqual(
            pa.attested_proof_owner(contract, self.peer_id), self.wallet
        )

    def test_a_node_with_no_identity_attests_nothing(self):
        # Best-effort by design: the proof is announced unattested rather than not at
        # all, and a reader declines to credit it, which is the right answer.
        contract = celaut_pb2.Contract()
        with unittest.mock.patch.object(pa, "get_node_public_key_hex", lambda: None):
            self.assertFalse(pa.attest_proof_ownership(contract, MNEMONIC))
        self.assertEqual(len(contract.xattrs), 0)

    def test_an_attestation_is_covered_by_the_peer_signature(self):
        # It rides in the proof's xattrs, which the digest already covers through
        # reputation_proofs -- so stripping one breaks the announcement rather than
        # silently costing the peer the reputation it can prove.
        peer = celaut_pb2.Peer()
        peer.reputation_proofs.append(self._proof())
        with_attestation = ni.canonical_peer_content_digest(peer)

        del peer.reputation_proofs[0].xattrs["owner_signature"]
        self.assertNotEqual(with_attestation, ni.canonical_peer_content_digest(peer))

if __name__ == "__main__":
    unittest.main()
