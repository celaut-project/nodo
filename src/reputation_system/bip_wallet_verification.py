"""BIP-39/BIP-32 derivation and signing for node identity (#236) and the reputation proof.

Signatures are Schnorr over secp256k1 (``src.utils.ergo_schnorr``), not ECDSA -- the
off-chain encoding ChainCash/Basis sign and an ErgoScript reserve contract verifies, not
an on-chain P2PK spending proof (see that module). The key is derived on Ergo's path and
denotes an Ergo P2PK proposition, so what it signs should be the kind of object that
ecosystem can check; the previous SHA-256 + ECDSA scheme used the right key on the right
curve to produce a signature no Ergo contract could check, and no Ergo tool could have
produced.
"""
from mnemonic import Mnemonic
from bip32 import BIP32, HARDENED_INDEX
import ecdsa
import binascii

from src.utils import ergo_schnorr

# Ergo's SLIP-44 derivation path. Reused as-is for the node's identity keypair
# (see src/reputation_system/node_identity.py) so that a node whose identity mnemonic
# is the same as its ledgers.ergo.WALLET_MNEMONIC gets the same key for both, with no
# extra derivation step.
ERGO_DERIVATION_PATH = "m/44'/429'/0'/0/0"

def __bip32_derive_key(bip32: BIP32, derivation_path: str):
    """
    Derives private and public keys using a BIP32 object and a derivation path.

    :param bip32: BIP32 object initialized with a seed.
    :param derivation_path: String representing the derivation path.
    :return: Tuple containing the private key and public key.
    """
    # Convert the derivation path into a list of indices
    indices = [int(x[:-1]) + HARDENED_INDEX if x.endswith("'") else int(x) for x in derivation_path.split('/')[1:]]
    # Derive the private and public keys from the indices
    privkey = bip32.get_privkey_from_path(indices)
    pubkey = bip32.get_pubkey_from_path(indices)
    return privkey, pubkey


def derive_keypair(mnemonic_phrase: str, derivation_path: str = ERGO_DERIVATION_PATH):
    """
    Derive the (private key bytes, 33-byte SEC-compressed public key) for a mnemonic.

    Single BIP-39 -> BIP-32 derivation shared by :func:`derive_compressed_pubkey` and
    :func:`bip_schnorr_sign`, so signing a message costs one PBKDF2 rather than two.
    Pure Python: no JVM/Ergo node needed, unlike the AppKit-based
    ``contracts.ergo.utils.get_public_key`` -- this is what lets a node derive its
    identity keypair (node_identity.py) from first boot.
    """
    mnemo = Mnemonic("english")
    if not mnemo.check(mnemonic_phrase):
        raise ValueError("Invalid mnemonic phrase.")

    seed = mnemo.to_seed(mnemonic_phrase, passphrase="")
    bip32 = BIP32.from_seed(seed)
    return __bip32_derive_key(bip32, derivation_path)


def derive_compressed_pubkey(mnemonic_phrase: str, derivation_path: str = ERGO_DERIVATION_PATH) -> bytes:
    """The 33-byte SEC-compressed public key for a mnemonic. See :func:`derive_keypair`."""
    _, pubkey = derive_keypair(mnemonic_phrase, derivation_path)
    return pubkey


def derive_signing_key(mnemonic_phrase: str, derivation_path: str = ERGO_DERIVATION_PATH):
    """The ``ecdsa.SigningKey`` for a mnemonic, ready to sign. See :func:`derive_keypair`."""
    private_key_bytes, _ = derive_keypair(mnemonic_phrase, derivation_path)
    return ecdsa.SigningKey.from_string(private_key_bytes, curve=ecdsa.SECP256k1)


def _message_bytes(message: str) -> bytes:
    """The bytes a signature is computed over.

    Fed straight into the challenge, with no pre-hash: Ergo's Schnorr takes the message
    itself, and adding a SHA-256 in front (as the previous ECDSA scheme had to, since
    ECDSA signs a digest) would define a nodo-only variant of the scheme -- the exact
    thing this stopped doing.
    """
    return message.encode("utf-8")


def sign_with_key(signing_key, message: str) -> str:
    """Sign ``message`` with an already-derived signing key, Ergo-style.

    Takes an ``ecdsa.SigningKey`` -- unchanged from when this signed ECDSA -- because that
    object carries both the private scalar and the public key, and callers cache it
    (``node_identity._cached_keypair``). Only the scheme underneath changed.
    """
    public_key = signing_key.get_verifying_key().to_string("compressed")
    signature = ergo_schnorr.sign(
        message=_message_bytes(message),
        private_key=signing_key.privkey.secret_multiplier,
        public_key=public_key,
    )
    return binascii.hexlify(signature).decode()


def bip_schnorr_sign(mnemonic_phrase: str, message: str) -> str:
    """
    Signs a message with Ergo's Schnorr scheme, using the Ergo derivation path.

    :param mnemonic_phrase: BIP-39 mnemonic phrase.
    :param message: Message to be signed.
    :return: 65-byte signature in hexadecimal format.
    """
    return sign_with_key(derive_signing_key(mnemonic_phrase), message)


def bip_schnorr_verify(message: str, signature_hex: str, public_key_hex: str) -> bool:
    """
    Verifies an Ergo Schnorr signature against a compressed public key.

    :param message: Original message that was signed.
    :param signature_hex: 65-byte signature in hexadecimal format.
    :param public_key_hex: Corresponding 33-byte compressed public key, hex-encoded.
    :return: True if the signature is valid, False otherwise.
    """
    try:
        return ergo_schnorr.verify(
            message=_message_bytes(message),
            public_key=binascii.unhexlify(public_key_hex),
            signature=binascii.unhexlify(signature_hex),
        )
    except (binascii.Error, ValueError):
        return False


# P2PK propositionBytes are `0008cd` + 33-byte SEC-compressed public key.
_P2PK_PREFIX = bytes.fromhex("0008cd")


def public_key_from_proposition_bytes(proposition_bytes: bytes) -> bytes:
    """Extract the 33-byte compressed public key from raw P2PK propositionBytes."""
    if isinstance(proposition_bytes, str):
        proposition_bytes = bytes.fromhex(proposition_bytes.strip())
    if len(proposition_bytes) != len(_P2PK_PREFIX) + 33 or not proposition_bytes.startswith(_P2PK_PREFIX):
        raise ValueError("Not a P2PK propositionBytes value.")
    return proposition_bytes[len(_P2PK_PREFIX):]


def bip_schnorr_verify_proposition(proposition_bytes: bytes, message: str, signature_hex: str) -> bool:
    """
    Verify that ``signature_hex`` over ``message`` was produced by the owner of the
    P2PK ``proposition_bytes`` (raw ErgoTree stored in a reputation box R7).

    The public key is taken directly from the propositionBytes, so this proves the
    peer controls exactly that R7 owner — the ownership-challenge counterpart of
    :func:`bip_schnorr_sign`. Returns ``False`` for any malformed input or bad signature.
    """
    try:
        compressed = public_key_from_proposition_bytes(proposition_bytes)
    except (ValueError, TypeError):
        return False
    try:
        return ergo_schnorr.verify(
            message=_message_bytes(message),
            public_key=compressed,
            signature=binascii.unhexlify(signature_hex),
        )
    except (binascii.Error, ValueError):
        return False
