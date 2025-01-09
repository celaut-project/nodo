from mnemonic import Mnemonic
from bip32 import BIP32, HARDENED_INDEX
import ecdsa
import hashlib
import binascii

def bip32_derive_key(bip32: BIP32, derivation_path: str):
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

def bip_ecdsa_sign(mnemonic_phrase: str, message: str) -> str:
    """
    Signs a message using ECDSA with the specified derivation path.

    :param mnemonic_phrase: BIP-39 mnemonic phrase.
    :param message: Message to be signed.
    :return: Signature in hexadecimal format.
    """
    # Validate the mnemonic phrase
    mnemo = Mnemonic("english")
    if not mnemo.check(mnemonic_phrase):
        raise ValueError("Invalid mnemonic phrase.")

    # Generate the seed from the mnemonic phrase
    seed = mnemo.to_seed(mnemonic_phrase, passphrase="")

    # Initialize BIP32 with the seed
    bip32 = BIP32.from_seed(seed)

    # Define the derivation path for Ergo platform
    derivation_path = "m/44'/429'/0'/0/0"

    # Obtain private and public keys
    private_key_bytes, _ = bip32_derive_key(bip32, derivation_path)

    # Load the private key in the appropriate format for ecdsa
    sk = ecdsa.SigningKey.from_string(private_key_bytes, curve=ecdsa.SECP256k1)

    # Hash the message using SHA-256
    msg_hash = hashlib.sha256(message.encode()).digest()

    # Sign the hash of the message using ECDSA
    signature = sk.sign(msg_hash)

    # Return the signature in hexadecimal format
    return binascii.hexlify(signature).decode()

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

def get_public_key_hex(mnemonic_phrase: str) -> str:
    """
    Obtains the public key in hexadecimal format from the mnemonic phrase.

    :param mnemonic_phrase: BIP-39 mnemonic phrase.
    :return: Public key in hexadecimal format.
    """
    # Validate the mnemonic phrase
    mnemo = Mnemonic('english')
    if not mnemo.check(mnemonic_phrase):
        raise ValueError("Invalid mnemonic phrase.")

    # Generate the seed from the mnemonic phrase
    seed = mnemo.to_seed(mnemonic_phrase, passphrase="")

    # Initialize BIP32 with the seed
    bip32 = BIP32.from_seed(seed)

    # Define the derivation path for Ergo platform
    derivation_path = "m/44'/429'/0'/0/0"

    # Obtain private and public keys
    _, public_key_bytes = bip32_derive_key(bip32, derivation_path)

    # Return the public key in hexadecimal format
    return binascii.hexlify(public_key_bytes).decode()
