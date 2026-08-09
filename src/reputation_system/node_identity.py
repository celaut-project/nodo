"""
Node identity keypair (issue #236): a node's own secp256k1 keypair, derived in pure
Python from a BIP-39 mnemonic. Used as the ``peer_id`` a node presents to others and
to sign its ``GetPeerInfo`` response (``Peer.public_key`` / ``Peer.signature``).

A node has exactly ONE mnemonic, ``ledgers.ergo.WALLET_MNEMONIC``, generated on first
config load when unset. It is both the node's wallet and its identity, so a reputation
proof published later is already tied to this node's identity for free, and the
identity can never change underneath the peers that recorded it.
"""
import hashlib
import string
from functools import lru_cache
from typing import Optional

import ecdsa

from src.reputation_system.bip_wallet_verification import (
    bip_ecdsa_verify_proposition,
    derive_keypair,
    sign_with_key,
)
from src.utils.config import ConfigManager

# P2PK propositionBytes are `0008cd` + 33-byte SEC-compressed public key (see
# bip_wallet_verification._P2PK_PREFIX). A reputation proof's R7 owner is stored in
# exactly this form, so comparing it against an announced identity is a plain prefix
# + the raw public_key hex -- no separate encoding of our own.
_P2PK_PREFIX_HEX = "0008cd"

# A SEC-compressed secp256k1 public key is 33 bytes -> 66 hex characters.
_PUBLIC_KEY_HEX_LENGTH = 66
_HEX_DIGITS = frozenset(string.hexdigits.lower())


def normalize_public_key_hex(public_key_hex: str) -> Optional[str]:
    """Canonical form of an announced public key, or None if it is not one.

    The public key doubles as the ``peer_id``, so it must have exactly one spelling:
    ``bytes.fromhex`` accepts uppercase and skips ASCII whitespace, which would let
    ``"02AB…"``, ``"02ab…"`` and ``"02 ab…"`` all verify and each become a *separate*
    peer row for one node. Worse, R7 owners are compared lowercased
    (``proof_validation._decode_coll_byte_hex``), so a non-lowercase id could never
    match its own reputation proof.
    """
    candidate = str(public_key_hex or "").strip().lower()
    if len(candidate) != _PUBLIC_KEY_HEX_LENGTH or not set(candidate) <= _HEX_DIGITS:
        return None
    return candidate


def get_identity_mnemonic() -> Optional[str]:
    """The mnemonic backing this node's identity keypair, or None if there is none.

    There is exactly ONE mnemonic in a node: ``ledgers.ergo.WALLET_MNEMONIC``, which is
    both its wallet and its identity. ConfigManager generates it on first load when
    unset, so this returns None only if the config was never loaded -- and never
    changes underfoot, which matters because the derived public key IS this node's
    ``peer_id``: a second source would let the node's identity silently change (and
    orphan its deposits and reputation network-wide) the moment a wallet was added.
    """
    mnemonic = str(ConfigManager().get("ledgers.ergo.WALLET_MNEMONIC", "") or "").strip()
    return mnemonic if mnemonic and mnemonic != "auto" else None


@lru_cache(maxsize=4)
def _cached_keypair(mnemonic: str):
    """Memoized BIP-39 -> BIP-32 derivation for one mnemonic: (pubkey hex, SigningKey).

    Deriving costs a PBKDF2-HMAC-SHA512 (2048 rounds) plus a BIP-32 walk, and signing
    a ``Peer`` used to pay it *twice* on every ``GetPeerInfo`` -- an unauthenticated
    RPC anyone can call. The identity key never changes for a given mnemonic, so cache
    it, keyed on the mnemonic itself so editing the config still takes effect.
    """
    private_key_bytes, pubkey = derive_keypair(mnemonic)
    return pubkey.hex(), ecdsa.SigningKey.from_string(private_key_bytes, curve=ecdsa.SECP256k1)


def get_node_public_key_hex() -> Optional[str]:
    """This node's identity public key, as the 33-byte compressed-key hex string."""
    mnemonic = get_identity_mnemonic()
    if not mnemonic:
        return None
    return _cached_keypair(mnemonic)[0]


def node_proposition_hex(public_key_hex: str) -> str:
    """The R7-shaped propositionBytes hex (``0008cd`` + pubkey) for a public key."""
    return _P2PK_PREFIX_HEX + public_key_hex


def _canonical_contract_message(contract) -> str:
    """Deterministic encoding of one ``Contract`` (a ledger plus its xattrs)."""
    xattrs = ";".join(
        f"{key}={bytes(contract.xattrs[key]).hex()}" for key in sorted(contract.xattrs)
    )
    return "~".join([
        contract.ledger.formal.hex(),
        ",".join(sorted(contract.ledger.tags)),
        contract.ledger.prose,
        xattrs,
    ])


def _canonical_contract(gas_price) -> str:
    """Deterministic encoding of one advertised payment contract and its price."""
    return "~".join([
        _canonical_contract_message(gas_price.contract),
        gas_price.gas_amount.n,
    ])


def _canonical_protocol(protocol) -> str:
    """Deterministic encoding of one ``Peer.Uri.Protocol``."""
    return "~".join([
        protocol.formal.hex(),
        ",".join(sorted(protocol.tags)),
        protocol.prose,
    ])


def _canonical_uri(uri) -> str:
    """Deterministic encoding of one advertised address and everything it declares."""
    protocol_stack = ";".join(sorted(_canonical_protocol(p) for p in uri.protocol_stack))
    return "~".join([
        uri.ip,
        str(uri.port),
        str(uri.expiry_unix_timestamp),
        _canonical_protocol(uri.transport),
        protocol_stack,
    ])


def canonical_peer_content_digest(peer) -> str:
    """A stable digest of everything a peer advertises about itself.

    The signature has to cover the *whole* advertisement, not just the addresses:
    ``payment_contracts`` is what decides where this node's money is sent, and
    ``gas_amount_per_call`` carries the advertised rates. Leaving them unsigned let
    anyone take a legitimately signed ``Peer``, swap in their own payment contract,
    and have it accepted and stored (``add_contract`` is INSERT OR IGNORE, so the
    forged contract lands *next to* the real one rather than replacing it). Each
    URI's own ``expiry_unix_timestamp`` and ``transport`` are included too, so
    neither can be stripped (making a soon-to-expire address look permanent, or a
    UDP endpoint look like a TCP one) nor extended in transit.

    ``reputation_proofs`` is covered as well. The proofs are validated against the
    peer's own id before being stored, so a forged one cannot enter the database --
    but the message is kept verbatim and republished on-chain, where a reader is told
    the signature vouches for it. Leaving them out would let a relay graft proofs onto
    (or strip them from) a claim that still verifies.

    Built field by field rather than from ``SerializeToString()``, which protobuf does
    not guarantee to be canonical (field order, unknown fields, non-minimal varints).
    Every repeated element is sorted so the digest does not depend on the order a node
    happened to enumerate things in.
    """
    uris = sorted(_canonical_uri(uri) for uri in peer.uri)
    contracts = sorted(_canonical_contract(gp) for gp in peer.payment_contracts)
    proofs = sorted(_canonical_contract_message(c) for c in peer.reputation_proofs)
    rates = ";".join(
        f"{key}={peer.gas_amount_per_call[key].n}" for key in sorted(peer.gas_amount_per_call)
    )

    canonical = "|".join(["/".join(uris), "/".join(contracts), "/".join(proofs), rates])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_peer_payload(public_key_hex: str, ts: int, content_digest: str) -> str:
    """
    The exact string a ``Peer`` signature is computed over.

    ``content_digest`` comes from :func:`canonical_peer_content_digest`, so the
    signature covers every advertised address (with its expiry and transport), the
    payment contracts and the rates at once, and any field added later is covered as
    soon as the digest accounts for it.
    """
    return f"{public_key_hex}|{ts}|{content_digest}"


def sign_peer_payload(payload: str) -> Optional[str]:
    """Sign ``payload`` with this node's identity key, or None if none is configured."""
    mnemonic = get_identity_mnemonic()
    if not mnemonic:
        return None
    return sign_with_key(_cached_keypair(mnemonic)[1], payload)


def verify_peer_payload(public_key_hex: str, payload: str, signature_hex: str) -> bool:
    """Verify ``signature_hex`` over ``payload`` was produced by ``public_key_hex``.

    Rejects any public key that is not already in canonical form, so a caller cannot
    end up storing a non-canonical spelling as a ``peer_id``.
    """
    if normalize_public_key_hex(public_key_hex) != public_key_hex or not signature_hex:
        return False
    try:
        proposition_bytes = bytes.fromhex(node_proposition_hex(public_key_hex))
    except (ValueError, TypeError):
        return False
    return bip_ecdsa_verify_proposition(proposition_bytes, payload, signature_hex)
