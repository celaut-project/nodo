"""
Node identity keypair (issue #236): a node's own secp256k1 keypair, derived in pure
Python from a BIP-39 mnemonic. Used as the ``peer_id`` a node presents to others and
to sign its ``GetPeerInfo`` response (``Peer.public_key`` / ``Peer.signature``).

The identity mnemonic is ``ledgers.ergo.WALLET_MNEMONIC`` when configured, so a node's
identity and its Ergo wallet are the same key and a reputation proof published later
is already tied to this node's identity "for free". A node with no Ergo wallet
configured falls back to ``network.NODE_MNEMONIC`` (auto-generated on first boot, see
ConfigManager), so identity does not depend on Ergo being set up at all.
"""
from typing import List, Optional

from src.reputation_system.bip_wallet_verification import (
    bip_ecdsa_sign,
    bip_ecdsa_verify_proposition,
    derive_compressed_pubkey,
)
from src.utils.config import ConfigManager

# P2PK propositionBytes are `0008cd` + 33-byte SEC-compressed public key (see
# bip_wallet_verification._P2PK_PREFIX). A reputation proof's R7 owner is stored in
# exactly this form, so comparing it against an announced identity is a plain prefix
# + the raw public_key hex -- no separate encoding of our own.
_P2PK_PREFIX_HEX = "0008cd"


def get_identity_mnemonic() -> Optional[str]:
    """The mnemonic backing this node's identity keypair, or None if none is set yet.

    ``network.NODE_MNEMONIC`` is auto-generated on first config load (like a ledger
    wallet mnemonic), so this is only None when the config has not been loaded at all.
    """
    config = ConfigManager()
    wallet_mnemonic = str(config.get("ledgers.ergo.WALLET_MNEMONIC", "") or "").strip()
    if wallet_mnemonic and wallet_mnemonic != "auto":
        return wallet_mnemonic
    node_mnemonic = str(config.get("network.NODE_MNEMONIC", "") or "").strip()
    return node_mnemonic if node_mnemonic and node_mnemonic != "auto" else None


def get_node_public_key_hex() -> Optional[str]:
    """This node's identity public key, as the 33-byte compressed-key hex string."""
    mnemonic = get_identity_mnemonic()
    if not mnemonic:
        return None
    return derive_compressed_pubkey(mnemonic).hex()


def node_proposition_hex(public_key_hex: str) -> str:
    """The R7-shaped propositionBytes hex (``0008cd`` + pubkey) for a public key."""
    return _P2PK_PREFIX_HEX + public_key_hex


def canonical_peer_payload(
    public_key_hex: str,
    ts: int,
    seq: int,
    uris: List[str],
    estimated_invalid_after: int = 0,
) -> str:
    """
    The exact string a ``Peer`` signature is computed over.

    Protobuf serialization is not canonical (field order, unknown fields, non-minimal
    varints), so the signed payload is this explicit, deterministic encoding instead
    of ``SerializeToString()``. URIs are sorted so the signature does not depend on
    the order a node happened to enumerate its interfaces in.

    ``estimated_invalid_after`` is part of the signed payload so it cannot be
    stripped (making a soon-to-expire address look permanent) or extended (keeping
    peers pinned to an address the signer knows is about to change).
    """
    return f"{public_key_hex}|{ts}|{seq}|{','.join(sorted(uris))}|{estimated_invalid_after}"


def sign_peer_payload(payload: str) -> Optional[str]:
    """Sign ``payload`` with this node's identity key, or None if none is configured."""
    mnemonic = get_identity_mnemonic()
    if not mnemonic:
        return None
    return bip_ecdsa_sign(mnemonic_phrase=mnemonic, message=payload)


def verify_peer_payload(public_key_hex: str, payload: str, signature_hex: str) -> bool:
    """Verify ``signature_hex`` over ``payload`` was produced by ``public_key_hex``."""
    if not public_key_hex or not signature_hex:
        return False
    try:
        proposition_bytes = bytes.fromhex(node_proposition_hex(public_key_hex))
    except (ValueError, TypeError):
        return False
    return bip_ecdsa_verify_proposition(proposition_bytes, payload, signature_hex)
