"""
Node identity keypair (issue #236): a node's own Ed25519 keypair, derived from a BIP-39
mnemonic of its own. Used as the ``peer_id`` a node presents to others and to sign its
``GetPeerInfo`` response (``Peer.public_key`` / ``Peer.signature``).

The identity is not a wallet on any ledger. It used to be one -- the node signed with
``ledgers.ergo.WALLET_MNEMONIC``, and a reputation proof's R7 owner *was* its
``peer_id`` -- which made the comparison free but bound the identity to a key that has
to be spendable on Ergo forever: R7 is the reputation contract's spending clause
(``INPUTS.exists { b.propositionBytes == SELF.R7[Coll[Byte]].get }``), so it can never
hold anything but an Ergo proposition. That made Ergo a dependency of the peer-to-peer
layer, down to a node with no wallet being unable to serve or dial at all.

What ties an identity to a ledger now is an attestation: a wallet on that ledger signs
this node's ``peer_id``, and the pair travels in ``Peer.ledger_attestations``. A reader
checks two links instead of comparing bytes -- the R7 owner is the attested wallet, and
that wallet signed this ``peer_id`` -- which is the same fact, verifiable from the
proof box alone with no round-trip to the node, and it holds for a second ledger
without privileging the first. Payment already had this shape
(``Peer.payment_contracts`` is a menu), and reputation and identity now match it.

There is exactly ONE identity mnemonic per node, ``identity.MNEMONIC``, generated on
first config load when unset. It never changes underneath the peers that recorded it,
and it is independent of every ``ledgers.*.WALLET_MNEMONIC``: adding, removing or
rotating a wallet leaves the node's name alone.
"""
import itertools
import string
from functools import lru_cache
from typing import Final, NamedTuple, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from mnemonic import Mnemonic

from protos import celaut_pb2
from src.reputation_system.bip_wallet_verification import (
    bip_schnorr_sign,
    bip_schnorr_verify_proposition,
    derive_compressed_pubkey,
)
from src.utils import ergo_schnorr
from src.utils.config import ConfigManager

# P2PK propositionBytes are `0008cd` + 33-byte SEC-compressed public key (see
# bip_wallet_verification._P2PK_PREFIX). A reputation proof's R7 owner is stored in
# exactly this form, so an attested Ergo wallet is compared against it as a plain
# prefix + the wallet's public key hex -- no separate encoding of our own.
_P2PK_PREFIX_HEX = "0008cd"

# A raw Ed25519 public key is 32 bytes -> 64 hex characters.
_PUBLIC_KEY_HEX_LENGTH = 64
_HEX_DIGITS = frozenset(string.hexdigits.lower())

# Where the identity mnemonic lives. Deliberately not under `ledgers`: an identity that
# reads its key out of one ledger's section is an identity that ledger owns.
IDENTITY_MNEMONIC_KEY: Final[str] = "identity.MNEMONIC"

# Ed25519 takes a 32-byte seed, and BIP-39 yields 64. Rather than truncate -- which
# would make the identity key a prefix of anything else derived from that seed -- the
# seed is hashed with a personalisation string, so the same mnemonic could back another
# key for another purpose without either being derivable from the other. Blake2b for
# the same reason the peer digest uses it: one hash family across the identity path.
_SEED_PERSONALISATION: Final[bytes] = b"celaut-id"

# Domain separation for a ledger attestation, so a wallet signature made to vouch for a
# peer_id can never be replayed as a signature over something else that wallet signs
# (an IOU note, a reputation payload) and the other way round.
_ATTESTATION_PREFIX: Final[str] = "celaut-ledger-attestation:"


class SignatureSchemeComponent(NamedTuple):
    """One building block of a signature scheme, in celaut's tags/prose/formal shape.

    ``formal`` belongs to the component it describes, not to the scheme around it: it
    is what :func:`_same_component` compares *first*, so a single value shared across
    every component would make them all interchangeable -- and a peer repeating that
    one value on however many components would match whatever its tags said, which is
    exactly what the exact-tag-set rule exists to refuse.

    It defaults to empty because no block of this node's scheme has a machine-readable
    artifact to point at yet, the same reason it is empty on the Ergo ledger
    (``reputation_system/envs.py``). When one gets a specification document, or the
    content hash of a verifier service, it goes on that entry alone and becomes the
    part that decides for it.
    """

    tags: Tuple[str, ...]
    prose: str
    formal: bytes = b""


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
# Order carries no meaning (curve, algorithm, hash, ledger convention only because
# that is the order a reader meets them in below); each entry is a
# :class:`SignatureSchemeComponent`, carrying its own ``formal``.
# Prose is read by whoever receives the announcement, so each block states itself and
# nothing about this implementation of it: a reader holding only the Peer message --
# off a gRPC response, or off an Ergo register -- cannot follow a path into some
# repository, and naming other projects only moves the question along ("and what is
# that?"). Same reason envs.PROSE describes the Ergo system and not nodo's client for
# it. Until `formal` points at a specification this text IS the specification, so it
# says everything a verification has to be written from and stands on its own.
SIGNATURE_SCHEME_COMPONENTS: Final[Tuple[SignatureSchemeComponent, ...]] = (
    SignatureSchemeComponent(
        ("ed25519",),
        "EdDSA over edwards25519, as specified in RFC 8032 (PureEdDSA, no context, no "
        "pre-hash). Private key: 32 uniformly random bytes. Public key: the 32-byte "
        "compressed encoding of the corresponding curve point, lowercase hex. "
        "Signature: the 64 bytes R || S of RFC 8032 section 5.1.6, lowercase hex, "
        "verified by the procedure in section 5.1.7. Message: the payload bytes, "
        "signed as given -- the algorithm hashes internally, so nothing pre-hashes "
        "them here.",
    ),
)

# Comparing two schemes is a search for a one-to-one pairing between their components
# (see :func:`same_signature_scheme`), which is factorial in a number a *peer* chooses.
# The length check there means the only comparison this node actually runs is against
# its own single-component scheme, so the search is bounded today -- but the function is
# a general ``(scheme_a, scheme_b) -> bool``, and nothing stops a later caller from
# handing it two peer schemes. Five leaves room for a scheme with one more building
# block than ours at 120 pairings; twelve would be 479 million, so past the cap a
# scheme is refused rather than computed. Configurable because the ceiling is a policy
# about what this node is willing to spend, not a fact about cryptography.
MAX_SIGNATURE_SCHEME_COMPONENTS_KEY: Final[str] = (
    "communication.MAX_SIGNATURE_SCHEME_COMPONENTS"
)
DEFAULT_MAX_SIGNATURE_SCHEME_COMPONENTS: Final[int] = 5


def _max_signature_scheme_components() -> int:
    """The configured cap, read per call so raising it needs no restart.

    Anything unreadable falls back to the default, deliberately catching everything:
    this is a safety bound consulted from the peer-registration path, and a node with
    no config file yet (or a malformed one) must fail to *read the cap*, not fail to
    decide whether it speaks a peer's scheme. A non-positive value falls back too --
    a cap of zero would refuse every scheme, including this node's own.
    """
    try:
        configured = int(ConfigManager().get(
            MAX_SIGNATURE_SCHEME_COMPONENTS_KEY, DEFAULT_MAX_SIGNATURE_SCHEME_COMPONENTS
        ))
    except Exception:
        return DEFAULT_MAX_SIGNATURE_SCHEME_COMPONENTS
    return configured if configured > 0 else DEFAULT_MAX_SIGNATURE_SCHEME_COMPONENTS


def node_signature_scheme():
    """This node's own scheme descriptor, as a fresh ``Peer.SignatureScheme``.

    Built per call rather than kept as a module constant so no caller can mutate the
    node's own declaration through the object it was handed.
    """
    scheme = celaut_pb2.Peer.SignatureScheme()
    for component in SIGNATURE_SCHEME_COMPONENTS:
        scheme.components.add(
            tags=list(component.tags), prose=component.prose, formal=component.formal
        )
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


def _component_is_declared(component) -> bool:
    """Whether a component names what it is at all.

    ``tags`` or ``formal``; either alone is enough, and a component carrying both is
    better still. Neither is not a building block this node can reason about: ``prose``
    is human text it has no way to judge, so a component holding only prose -- or
    nothing -- states nothing a comparison could act on. Something is missing from it,
    and the conservative answer to "is this the cryptography I speak?" is no.

    It also keeps the relation reflexive: with no tags and no formal, a component would
    otherwise fail to match a byte-identical copy of itself.
    """
    return bool(component.tags) or bool(bytes(component.formal))


def _same_component(a, b) -> bool:
    """Whether two ``SignatureScheme.Protocol`` entries name the same building block.

    ``formal`` first, as the strictest and most machine-readable identity, and the tags
    only when neither side has one -- and there as a **set**, never by intersection.
    The tags within one component are meant to be synonyms for the one thing it names
    (``["ed25519", "edwards25519"]``), but nothing in the message says so: this node
    cannot tell that ``edwards25519`` restates the component it sits in while ``ed25519ph``
    beside ``ed25519`` names a second, different thing -- the pre-hashed variant of RFC
    8032, whose signatures do not verify under the pure one. Only one of the two guesses
    is safe,
    and the unsafe one accepts a signer whose signatures this node cannot verify -- so
    an extra tag makes it a different component, exactly as an extra tag made it a
    different scheme under the flat-set rule this replaced.

    ``formal`` is the way out of that rigidity rather than a workaround for it: once a
    component points at a specification, that specification decides on its own and the
    vocabulary stops mattering.

    ``prose`` is not compared at all: it is human text this node has no way to judge,
    and making it decisive would refuse a peer for rewording a sentence.
    """
    formal_a, formal_b = bytes(a.formal), bytes(b.formal)
    if formal_a or formal_b:
        return formal_a == formal_b
    return set(a.tags) == set(b.tags)


def same_signature_scheme(a, b) -> bool:
    """Whether two scheme descriptors denote the same cryptography.

    The comparison a node makes on its own, and the one place a
    ``(scheme_a, scheme_b) -> bool`` equivalence service would be called from instead
    -- deciding that two differently-worded descriptors mean the same scheme is
    exactly the judgement such a service exists to make, and until one is asked this
    node has to answer conservatively.

    A scheme is an unordered stack of components (see ``Peer.SignatureScheme`` in
    celaut.proto), so this asks for a one-to-one pairing between the two schemes'
    components, not a positional comparison. Within a pair, matching is
    :func:`_same_component`'s (``formal`` first, an exact set of tags otherwise);
    across the whole scheme, the pairing must be total. A peer declaring an extra
    component, or missing one, is a different scheme even if every paired component
    matches -- same reasoning as the flat-tag-set rule this replaced (a peer declaring
    ``["ed25519", "ed25519ph"]`` shares a tag with this node and signs something this
    node cannot read -- same curve, different message convention -- so "at least one
    shared component" is exactly the answer that must not be given here), just expressed
    per-component instead of over one flat list.

    Three things are refused before the pairing is searched for, each of them a "no"
    in its own right rather than an optimization:

    * **Different cardinality.** Nothing to pair; see above.
    * **More components than the configured cap**
      (``communication.MAX_SIGNATURE_SCHEME_COMPONENTS``). The search is factorial in
      a length a peer chooses. Today the only comparison this node runs is against its
      own single-component scheme, so the cardinality check already bounds it -- the cap
      is what keeps that true if this ever compares two peers' schemes to each other.
    * **A component that declares neither tags nor formal** on either side; see
      :func:`_component_is_declared`.
    """
    a_components, b_components = list(a.components), list(b.components)
    if len(a_components) != len(b_components):
        return False

    if len(a_components) > _max_signature_scheme_components():
        return False

    if not all(_component_is_declared(c) for c in a_components + b_components):
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
    ``"AB…"``, ``"ab…"`` and ``"a b…"`` all verify and each become a *separate* peer
    row for one node.
    """
    candidate = str(public_key_hex or "").strip().lower()
    if len(candidate) != _PUBLIC_KEY_HEX_LENGTH or not set(candidate) <= _HEX_DIGITS:
        return None
    return candidate


def get_identity_mnemonic() -> Optional[str]:
    """The mnemonic backing this node's identity keypair, or None if there is none.

    There is exactly ONE identity mnemonic in a node, ``identity.MNEMONIC``, and it is
    not any ledger's wallet. ConfigManager generates it on first load when unset, so
    this returns None only if the config was never loaded -- and it never changes
    underfoot, which matters because the derived public key IS this node's ``peer_id``:
    a second source would let the node's identity silently change (and orphan its
    deposits and reputation network-wide) the moment a wallet was added or removed.
    """
    mnemonic = str(ConfigManager().get(IDENTITY_MNEMONIC_KEY, "") or "").strip()
    return mnemonic if mnemonic and mnemonic != "auto" else None


@lru_cache(maxsize=4)
def _cached_keypair(mnemonic: str):
    """Memoized derivation for one mnemonic: ``(public key hex, Ed25519PrivateKey)``.

    Deriving costs a PBKDF2-HMAC-SHA512 (2048 rounds), and signing a ``Peer`` would pay
    it on every ``GetPeerInfo`` -- an unauthenticated RPC anyone can call. The identity
    key never changes for a given mnemonic, so cache it, keyed on the mnemonic itself
    so editing the config still takes effect.

    The BIP-39 seed is 64 bytes and Ed25519 wants 32, so it is hashed down rather than
    truncated: a truncation would make this key a prefix of whatever else that seed
    derives, while a personalised Blake2b leaves the two unrelated.
    """
    mnemo = Mnemonic("english")
    if not mnemo.check(mnemonic):
        raise ValueError("Invalid mnemonic phrase.")
    digest = hashes.Hash(hashes.BLAKE2b(64))
    digest.update(_SEED_PERSONALISATION + mnemo.to_seed(mnemonic, passphrase=""))
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(digest.finalize()[:32])
    public_key_hex = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    return public_key_hex, private_key


def get_node_public_key_hex() -> Optional[str]:
    """This node's identity public key, as the 32-byte raw Ed25519 key in hex."""
    mnemonic = get_identity_mnemonic()
    if not mnemonic:
        return None
    return _cached_keypair(mnemonic)[0]


def node_proposition_hex(wallet_public_key_hex: str) -> str:
    """The R7-shaped propositionBytes hex (``0008cd`` + pubkey) for an Ergo wallet key.

    This describes a *wallet*, never the node's identity: R7 is the reputation
    contract's spending clause, so only a key that can spend on Ergo belongs in it. A
    node's identity reaches that comparison through the attestation its Ergo wallet
    signed (:func:`attested_wallet_public_key`), not by being the same key.
    """
    return _P2PK_PREFIX_HEX + wallet_public_key_hex


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


def _canonical_attestation(attestation) -> str:
    """Deterministic encoding of one ledger attestation."""
    return "~".join([
        _canonical_protocol(attestation.ledger),
        attestation.public_key,
        attestation.signature,
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

    ``ledger_attestations`` is covered too. Each one already carries its own signature
    by the wallet it names, so a forged entry cannot survive
    :func:`attested_wallet_public_key` -- but a relay could still *strip* one, and a
    peer whose Ergo attestation went missing loses the reputation it can prove. Covering
    them makes the removal break the announcement instead.

    Built field by field rather than from ``SerializeToString()``, which protobuf does
    not guarantee to be canonical (field order, unknown fields, non-minimal varints).
    Every repeated element is sorted so the digest does not depend on the order a node
    happened to enumerate things in.

    ``signature_scheme`` is covered as well, which it has to be as soon as more than one
    scheme can be accepted: a relay that could re-label a signature as belonging to a
    different scheme -- one whose verification also accepts those bytes, or simply a
    weaker one -- would be re-labelling an authentication decision. Reading a scheme
    stays a local capability (see :func:`same_signature_scheme`), so nothing stops a
    node from plugging in a second verifier, and the digest must not be the thing that
    has to change when it does.
    """
    uris = sorted(_canonical_uri(uri) for uri in peer.uri)
    contracts = sorted(_canonical_contract(gp) for gp in peer.payment_contracts)
    proofs = sorted(_canonical_contract_message(c) for c in peer.reputation_proofs)
    attestations = sorted(
        _canonical_attestation(a) for a in peer.ledger_attestations
    )
    scheme = ";".join(
        sorted(_canonical_protocol(c) for c in peer.signature_scheme.components)
    )
    rates = ";".join(
        f"{key}={peer.mu_per_call[key].n}" for key in sorted(peer.mu_per_call)
    )

    canonical = "|".join([
        "/".join(uris),
        "/".join(contracts),
        "/".join(proofs),
        rates,
        "/".join(attestations),
        scheme,
    ])
    # Blake2b-256, the hash this node's own scheme names in its challenge and the one
    # Ergo uses: the digest is what a signature commits to, so keeping it on a different
    # hash family from everything else in the identity path would be the one remaining
    # nodo-only primitive. Nothing outside this function reads the value -- it is
    # recomputed by the verifier from the peer's own advertisement, never stored or
    # transmitted.
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
    return _cached_keypair(mnemonic)[1].sign(payload.encode("utf-8")).hex()


def verify_peer_payload(public_key_hex: str, payload: str, signature_hex: str) -> bool:
    """Verify ``signature_hex`` over ``payload`` was produced by ``public_key_hex``.

    Rejects any public key that is not already in canonical form, so a caller cannot
    end up storing a non-canonical spelling as a ``peer_id``.
    """
    if normalize_public_key_hex(public_key_hex) != public_key_hex or not signature_hex:
        return False
    try:
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(public_key_hex)
        )
        public_key.verify(bytes.fromhex(signature_hex), payload.encode("utf-8"))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def attestation_payload(peer_id: str) -> str:
    """The exact string a ledger wallet signs to vouch for ``peer_id``.

    Prefixed so the signature cannot be lifted into any other context that wallet signs
    -- a reputation payload, an IOU note -- nor one of those replayed as an attestation.
    The peer_id alone would be 64 hex characters, which is also the shape of plenty of
    other things an Ergo key is asked to sign.
    """
    return _ATTESTATION_PREFIX + peer_id


def declare_ledger_attestations(peer) -> None:
    """Attach this node's ledger attestations to ``peer``, replacing any already there.

    One per configured wallet: the wallet signs this node's ``peer_id``, so a reader
    holding the announcement can tie the identity to a key on that ledger without the
    two having to be the same key. That is what makes the identity independent of every
    ledger while a reputation proof or a payment can still be attributed to this node.

    A wallet whose mnemonic is unusable is skipped rather than fatal: the node has an
    identity regardless, and the ledger it could not sign for simply goes unattested --
    a reader then declines to credit that ledger's proofs to this peer, which is the
    right answer, and the peer-to-peer layer is unaffected.
    """
    del peer.ledger_attestations[:]
    peer_id = get_node_public_key_hex()
    if not peer_id:
        return

    payload = attestation_payload(peer_id)
    for ledger_name, mnemonic in _configured_wallets():
        try:
            signature = bip_schnorr_sign(mnemonic, payload)
            public_key_hex = _wallet_public_key_hex(mnemonic)
        except Exception:
            continue
        attestation = peer.ledger_attestations.add()
        attestation.ledger.tags.append(ledger_name)
        attestation.public_key = public_key_hex
        attestation.signature = signature


def attested_wallet_public_key(peer, ledger_tag: str) -> Optional[str]:
    """The wallet ``peer`` proved it holds on ``ledger_tag``, or None.

    The verification a reader runs before crediting anything on that ledger to this
    peer: the attestation has to be signed by the very wallet it names, over this
    peer's own id. An announcement carrying a wallet it cannot prove is worth exactly
    as much as one carrying none.

    Two attestations for one ledger are refused rather than picked between -- which
    wallet a proof belongs to would have no answer, and choosing is policy nobody has
    written.
    """
    peer_id = normalize_public_key_hex(peer.public_key)
    if not peer_id:
        return None

    candidates = [
        attestation
        for attestation in peer.ledger_attestations
        if ledger_tag in attestation.ledger.tags
    ]
    if len(candidates) != 1:
        return None

    attestation = candidates[0]
    wallet_public_key = str(attestation.public_key or "").strip().lower()
    if not wallet_public_key or not set(wallet_public_key) <= _HEX_DIGITS:
        return None
    try:
        proposition_bytes = bytes.fromhex(node_proposition_hex(wallet_public_key))
    except (ValueError, TypeError):
        return None
    if not bip_schnorr_verify_proposition(
        proposition_bytes, attestation_payload(peer_id), attestation.signature
    ):
        return None
    return wallet_public_key


def _configured_wallets():
    """``(ledger name, mnemonic)`` for every ledger this node holds a wallet on."""
    ledgers = ConfigManager().get("ledgers", {}) or {}
    if not isinstance(ledgers, dict):
        return
    for name, ledger in ledgers.items():
        if not isinstance(ledger, dict):
            continue
        mnemonic = str(ledger.get("WALLET_MNEMONIC") or "").strip()
        if mnemonic and mnemonic != "auto":
            yield str(name), mnemonic


@lru_cache(maxsize=4)
def _wallet_public_key_hex(mnemonic: str) -> str:
    """The 33-byte SEC-compressed wallet key for a mnemonic, hex. Cached like the identity."""
    return derive_compressed_pubkey(mnemonic).hex()
