"""Tying a reputation proof to the node that announced it (issue #236).

A node's identity is a keypair of its own, on no ledger (``src.identity.node_identity``).
A reputation proof's owner is a wallet, because Ergo's reputation contract names it in
R7 -- its spending clause, ``INPUTS.exists { b.propositionBytes == SELF.R7[Coll[Byte]]
.get }`` -- so R7 can hold nothing but an Ergo proposition, ever.

This is the bridge between the two: the wallet that published a proof signs the node's
``peer_id``, and the pair rides in that proof's own xattrs. A reader then goes from an
on-chain owner to a peer id in two verifiable steps rather than by comparing bytes,
which is what lets the identity be independent of every ledger while a proof stays
attributable.

It lives here, beside the wallet derivation it needs, rather than with the identity:
everything below is in Ergo's cryptography and Ergo's encodings, and an identity module
that reached for them would be an identity Ergo owns -- exactly what the split exists
to prevent. What crosses between the two is one string, ``attestation_payload``.
"""
from functools import lru_cache
from typing import Optional

from src.reputation_system.bip_wallet_verification import (
    bip_schnorr_sign,
    bip_schnorr_verify_proposition,
    derive_compressed_pubkey,
)
from src.identity.node_identity import (
    _HEX_DIGITS,
    attestation_payload,
    get_node_public_key_hex,
    normalize_public_key_hex,
)

# P2PK propositionBytes are `0008cd` + 33-byte SEC-compressed public key (see
# bip_wallet_verification._P2PK_PREFIX). A reputation proof's R7 owner is stored in
# exactly this form, so an attested wallet is compared against it as a plain prefix
# + the wallet's public key hex -- no separate encoding of our own.
_P2PK_PREFIX_HEX = "0008cd"


def node_proposition_hex(wallet_public_key_hex: str) -> str:
    """The R7-shaped propositionBytes hex (``0008cd`` + pubkey) for an Ergo wallet key.

    This describes a *wallet*, never the node's identity: R7 is the reputation
    contract's spending clause, so only a key that can spend on Ergo belongs in it. A
    node's identity reaches that comparison through the attestation its Ergo wallet
    signed (:func:`attested_proof_owner`), not by being the same key.
    """
    return _P2PK_PREFIX_HEX + wallet_public_key_hex


def attest_proof_ownership(contract, mnemonic: str) -> bool:
    """Record on ``contract`` that the wallet behind ``mnemonic`` published it.

    The wallet signs this node's ``peer_id`` and both halves go into the proof's own
    xattrs, so the claim travels with the thing it is about. A reader then ties an
    on-chain owner to a peer id without the two having to be the same key -- which is
    what lets the identity be independent of every ledger while a proof is still
    attributable.

    Per proof rather than per peer: a node holds as many proofs as it likes (issue
    #281) and nothing says they share an owner, so an attestation on the announcement
    could not describe two proofs on one ledger under different wallets.

    Returns whether anything was written. An unusable mnemonic, or a node with no
    identity yet, leaves the proof unattested rather than failing: a reader then
    declines to credit it, which is the right answer, and nothing else is affected.
    """
    peer_id = get_node_public_key_hex()
    if not peer_id or not mnemonic:
        return False
    try:
        signature = bip_schnorr_sign(mnemonic, attestation_payload(peer_id))
        public_key_hex = _wallet_public_key_hex(mnemonic)
    except Exception:
        return False

    from src.utils.contract_xattrs import set_owner_attestation

    set_owner_attestation(contract, public_key_hex, signature)
    return True


def attested_proof_owner(contract, peer_id: str) -> Optional[str]:
    """The wallet ``peer_id`` proved published ``contract``, or None.

    The check a reader runs before crediting a proof to a peer: the attestation has to
    be signed by the very wallet it names, over that peer's own id. A proof announcing
    an owner it cannot prove is worth exactly as much as one announcing none -- naming
    someone else's wallet is free, signing with it is not.
    """
    from src.utils.contract_xattrs import get_owner_attestation

    peer_id = normalize_public_key_hex(peer_id)
    if not peer_id:
        return None

    public_key, signature = get_owner_attestation(contract)
    public_key = public_key.strip().lower()
    if not public_key or not signature or not set(public_key) <= _HEX_DIGITS:
        return None
    try:
        proposition_bytes = bytes.fromhex(node_proposition_hex(public_key))
    except (ValueError, TypeError):
        return None
    if not bip_schnorr_verify_proposition(
        proposition_bytes, attestation_payload(peer_id), signature
    ):
        return None
    return public_key


@lru_cache(maxsize=4)
def _wallet_public_key_hex(mnemonic: str) -> str:
    """The 33-byte SEC-compressed wallet key for a mnemonic, hex. Cached like the identity."""
    return derive_compressed_pubkey(mnemonic).hex()
