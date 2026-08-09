from mnemonic import Mnemonic
from bip32 import BIP32, HARDENED_INDEX
import ecdsa
import hashlib
import binascii

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
    :func:`bip_ecdsa_sign`, so signing a message costs one PBKDF2 rather than two.
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


def sign_with_key(signing_key, message: str) -> str:
    """Sign ``message`` (SHA-256 then ECDSA) with an already-derived signing key."""
    msg_hash = hashlib.sha256(message.encode()).digest()
    return binascii.hexlify(signing_key.sign(msg_hash)).decode()


def bip_ecdsa_sign(mnemonic_phrase: str, message: str) -> str:
    """
    Signs a message using ECDSA with the specified derivation path.

    :param mnemonic_phrase: BIP-39 mnemonic phrase.
    :param message: Message to be signed.
    :return: Signature in hexadecimal format.
    """
    return sign_with_key(derive_signing_key(mnemonic_phrase), message)

def bip_ecdsa_verify(message: str, signature_hex: str, public_key_hex: str) -> bool:
    """
    Verifies an ECDSA signature.

    :param message: Original message that was signed.
    :param signature_hex: ECDSA signature in hexadecimal format.
    :param public_key_hex: Corresponding public key in hexadecimal format.
    :return: True if the signature is valid, False otherwise.
    """
    try:
        # Convert the signature and public key from hexadecimal to bytes
        signature_bytes = binascii.unhexlify(signature_hex)
        public_key_bytes = binascii.unhexlify(public_key_hex)

        # Import the public key in the appropriate format for ecdsa
        vk = ecdsa.VerifyingKey.from_string(public_key_bytes, curve=ecdsa.SECP256k1)

        # Hash the message using SHA-256
        msg_hash = hashlib.sha256(message.encode()).digest()

        # Verify the signature
        return vk.verify(signature_bytes, msg_hash)

    except (binascii.Error, ValueError, ecdsa.BadSignatureError):
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


def bip_ecdsa_verify_proposition(proposition_bytes: bytes, message: str, signature_hex: str) -> bool:
    """
    Verify that ``signature_hex`` over ``message`` was produced by the owner of the
    P2PK ``proposition_bytes`` (raw ErgoTree stored in a reputation box R7).

    The public key is taken directly from the propositionBytes, so this proves the
    peer controls exactly that R7 owner — the ownership-challenge counterpart of
    :func:`bip_ecdsa_sign`. Returns ``False`` for any malformed input or bad signature.
    """
    try:
        compressed = public_key_from_proposition_bytes(proposition_bytes)
        vk = ecdsa.VerifyingKey.from_string(
            compressed, curve=ecdsa.SECP256k1, valid_encodings=["compressed"]
        )
        msg_hash = hashlib.sha256(message.encode()).digest()
        return vk.verify(binascii.unhexlify(signature_hex), msg_hash)
    except (binascii.Error, ValueError, ecdsa.BadSignatureError, ecdsa.MalformedPointError):
        return False
