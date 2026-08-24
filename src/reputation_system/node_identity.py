"""
Node identity keypair (issue #236): a node's own secp256k1 keypair, derived in pure
Python from a BIP-39 mnemonic. Used as the ``peer_id`` a node presents to others and
to sign its ``GetPeerInfo`` response (``Peer.public_key`` / ``Peer.signature``).

A node has exactly ONE mnemonic, ``ledgers.ergo.WALLET_MNEMONIC``, generated on first
config load when unset. It is both the node's wallet and its identity, so a reputation
proof published later is already tied to this node's identity for free, and the
identity can never change underneath the peers that recorded it.
"""
import itertools
import string
from functools import lru_cache
from typing import Final, Optional, Tuple

import ecdsa

from protos import celaut_pb2
from src.reputation_system.bip_wallet_verification import (
    bip_schnorr_verify_proposition,
    derive_keypair,
    sign_with_key,
)
from src.utils import ergo_schnorr
from src.utils.config import ConfigManager

# P2PK propositionBytes are `0008cd` + 33-byte SEC-compressed public key (see
# bip_wallet_verification._P2PK_PREFIX). A reputation proof's R7 owner is stored in
# exactly this form, so comparing it against an announced identity is a plain prefix
# + the raw public_key hex -- no separate encoding of our own.
_P2PK_PREFIX_HEX = "0008cd"

# A SEC-compressed secp256k1 public key is 33 bytes -> 66 hex characters.
_PUBLIC_KEY_HEX_LENGTH = 66
_HEX_DIGITS = frozenset(string.hexdigits.lower())

# The cryptography this node's identity signatures are in, announced on
# ``Peer.signature_scheme`` and demanded of every peer it registers.
#
# A scheme is an unordered stack of components (``Peer.SignatureScheme.components``,
# each a ``tags``/``prose``/``formal`` descriptor exactly like ``Uri.Protocol`` or
# ``Contract.Ledger``) -- one per building block, rather than a fixed set of named
# fields, so a future scheme with a different shape (hash-based, threshold, no curve
# at all) needs no proto migration to be expressed, only a different-length stack.
#
# A descriptor, not an id derived from one: celaut never names a tags/prose/formal
# component by a hash of itself (compare ``envs.ergo_ledger``, whose ``formal`` is
# even empty), and doing it here would invent a naming rule for signature schemes that
# nothing else in the protocol follows. The descriptor IS the name, and deciding
# whether two of them denote the same thing is a comparison -- one a service of the
# shape ``(scheme_a, scheme_b) -> bool`` can eventually make better than this node
# does (see :func:`same_signature_scheme`).
#
# ``formal`` is empty on every component for the same reason it is empty on the Ergo
# ledger: there is no machine-readable artifact to point at yet. When there is one --
# a specification document, or the content hash of a verifier service -- it goes on
# that component and becomes the part that decides, which is why it is compared first.
#
# Order carries no meaning (curve, algorithm, hash, ledger convention only because
# that is the order a reader meets them in below); each entry is (tags, prose).
# Prose is read by whoever receives the announcement, so each block states itself and
# nothing about this implementation of it: a reader holding only the Peer message --
# off a gRPC response, or off an Ergo register -- cannot follow a path into some
# repository, and naming other projects only moves the question along ("and what is
# that?"). Same reason envs.PROSE describes the Ergo system and not nodo's client for
# it. Until `formal` points at a specification this text IS the specification, so it
# says everything a verification has to be written from and stands on its own.
SIGNATURE_SCHEME_COMPONENTS: Final[Tuple[Tuple[Tuple[str, ...], str], ...]] = (
    (
        ("secp256k1",),
        "The secp256k1 elliptic curve, with generator G and group order n. Private "
        "key: a scalar s in [1, n). Public key: the point P = s*G, encoded as its "
        "33-byte SEC-compressed form, lowercase hex.",
    ),
    (
        ("schnorr",),
        "A Schnorr signature over the curve named by the accompanying curve "
        "component. Message: the payload bytes, signed as given, with no pre-hash. "
        "Signature: the 65 bytes a || z, lowercase hex, where k is a nonce drawn "
        "uniformly from [1, n) for each signature, a is the 33-byte SEC-compressed "
        "form of k*G, and z is the 32-byte big-endian encoding of (k + e*s) mod n, e "
        "being the challenge named by the accompanying hash component. Valid if and "
        "only if z*G == a + e*P. A signer redraws k until the first byte of both e "
        "and z is < 0x80: for z that is required, since a set top bit would make it "
        "a negative scalar, and for e it makes the two's-complement and unsigned "
        "readings coincide, so the signature verifies under either.",
    ),
    (
        ("blake2b256",),
        "The challenge hash: e = blake2b256(a || m || P), its 32 bytes read as a "
        "two's-complement big-endian integer, so a digest whose first byte is >= "
        "0x80 denotes a negative e.",
    ),
    (
        ("ergo",),
        "The keypair is the one an Ergo P2PK proposition names, and this scheme is "
        "the sigma protocol those proofs are built on.",
    ),
)
SIGNATURE_SCHEME_FORMAL: Final[bytes] = b""


def node_signature_scheme():
    """This node's own scheme descriptor, as a fresh ``Peer.SignatureScheme``.

    Built per call rather than kept as a module constant so no caller can mutate the
    node's own declaration through the object it was handed.
    """
    scheme = celaut_pb2.Peer.SignatureScheme()
    for tags, prose in SIGNATURE_SCHEME_COMPONENTS:
        scheme.components.add(tags=list(tags), prose=prose, formal=SIGNATURE_SCHEME_FORMAL)
    return scheme


def declare_signature_scheme(peer, *, prose: bool = True) -> None:
    """Declare on ``peer`` which cryptography its ``public_key``/``signature`` are in.

    Called wherever this node signs an announcement: on the wire it is what lets a
    reader that speaks something else say so, instead of reporting a peer whose
    signature simply "does not verify".

    ``prose=False`` leaves out every component's description, which costs nothing
    over a gRPC response but is a kilobyte of an Ergo register a box pays storage
    rent on forever -- the whole budget, on its own (see
    ``tests/reputation_system/test_onchain_peer_object.py``). The tags stay: while
    ``formal`` is empty they are the whole machine-readable half of each component,
    and dropping them would leave a descriptor that says nothing.
    """
    peer.signature_scheme.CopyFrom(node_signature_scheme())
    if not prose:
        for component in peer.signature_scheme.components:
            component.ClearField("prose")


def _same_component(a, b) -> bool:
    """Whether two ``SignatureScheme.Protocol`` entries name the same building block.

    ``formal`` first, as the strictest and most machine-readable identity, and the
    tags only when neither side has one -- and there, *any* shared tag is enough:
    within one component the tags are synonyms for the one thing it names (e.g.
    ``["secp256k1", "K-256"]``), same idiom as ``Uri.Protocol``/``Network.tags``
    elsewhere. ``prose`` is not compared at all: it is human text this node has no
    way to judge, and making it decisive would refuse a peer for rewording a
    sentence.
    """
    formal_a, formal_b = bytes(a.formal), bytes(b.formal)
    if formal_a or formal_b:
        return formal_a == formal_b
    return bool(set(a.tags) & set(b.tags))


def same_signature_scheme(a, b) -> bool:
    """Whether two scheme descriptors denote the same cryptography.

    The comparison a node makes on its own, and the one place a
    ``(scheme_a, scheme_b) -> bool`` equivalence service would be called from instead
    -- deciding that two differently-worded descriptors mean the same scheme is
    exactly the judgement such a service exists to make, and until one is asked this
    node has to answer conservatively.

    A scheme is an unordered stack of components (see ``Peer.SignatureScheme`` in
    celaut.proto), so this asks for a one-to-one pairing between the two schemes'
    components, not a positional comparison -- schemes have a handful of components,
    so trying every permutation is cheap. Within a pair, matching is
    :func:`_same_component`'s (``formal`` first, tags as synonyms otherwise); across
    the whole scheme, the pairing must be total. A peer declaring an extra component,
    or missing one, is a different scheme even if every paired component matches --
    same reasoning as the flat-tag-set rule this replaced (a peer declaring
    ``["secp256k1", "bip340"]`` shares a tag with this node and signs something this
    node cannot read -- same curve, different algorithm -- so "at least one shared
    component" is exactly the answer that must not be given here), just expressed
    per-component instead of over one flat list.
    """
    a_components, b_components = list(a.components), list(b.components)
    if len(a_components) != len(b_components):
        return False
    return any(
        all(_same_component(x, y) for x, y in zip(a_components, permutation))
        for permutation in itertools.permutations(b_components)
    )


def speaks_our_signature_scheme(peer) -> bool:
    """Whether ``peer``'s announcement is signed with the one scheme this node verifies.

    The node implements no scheme negotiation and no second scheme: this is the whole
    of its handling of ``Peer.signature_scheme``. It has to be checked *before* the
    key and the signature are read, because what those two fields mean -- their
    length, their encoding, the verification procedure -- is exactly what the scheme
    decides.

    A descriptor with no components is the pre-field default rather than a wildcard:
    back when the field did not exist there was only one scheme an announcement could
    mean, so it resolves to this one, and a peer meaning anything else has to say so.
    """
    scheme = peer.signature_scheme
    if not scheme.components:
        return True
    return same_signature_scheme(scheme, node_signature_scheme())


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


def _canonical_contract(contract_rate) -> str:
    """Deterministic encoding of one advertised payment contract and its rate."""
    return "~".join([
        _canonical_contract_message(contract_rate.contract),
        contract_rate.mu_per_unit.n,
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
    ``mu_per_call`` carries the advertised rates. Leaving them unsigned let
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

    ``signature_scheme`` is deliberately NOT covered, and that holds only while
    :func:`speaks_our_signature_scheme` accepts exactly one scheme: altering the field
    can then turn a valid announcement into a refused one but never into an accepted
    one, which anyone able to alter it could do by corrupting any other byte anyway.
    Accepting a second scheme changes that -- a relay could re-label a signature as
    belonging to whichever accepted scheme is weakest -- so the scheme id has to enter
    this digest in the same commit that accepts one.
    """
    uris = sorted(_canonical_uri(uri) for uri in peer.uri)
    contracts = sorted(_canonical_contract(gp) for gp in peer.payment_contracts)
    proofs = sorted(_canonical_contract_message(c) for c in peer.reputation_proofs)
    rates = ";".join(
        f"{key}={peer.mu_per_call[key].n}" for key in sorted(peer.mu_per_call)
    )

    canonical = "|".join(["/".join(uris), "/".join(contracts), "/".join(proofs), rates])
    # Blake2b-256, Ergo's hash, for the same reason the signature is Ergo's Schnorr: this
    # digest is what the signature commits to, so keeping it on a different hash family
    # than everything else in the identity path would be the one remaining nodo-only
    # primitive. Nothing outside this function reads the value -- it is recomputed by the
    # verifier from the peer's own advertisement, never stored or transmitted.
    return ergo_schnorr.blake2b256(canonical.encode("utf-8")).hex()


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
    return bip_schnorr_verify_proposition(proposition_bytes, payload, signature_hex)
