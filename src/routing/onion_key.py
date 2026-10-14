"""The key of the onion service of the gateway, derived from the identity mnemonic.

The key is recoverable from ``identity.MNEMONIC`` alone: a node restored from its
mnemonic announces the same ``.onion`` name, and no key file has to be backed up.

It is not the identity key. The seed is hashed with its own personalisation string
(compare ``node_identity._SEED_PERSONALISATION``), so the two keys are unrelated and
neither can be computed from the other. Two protocols never share a key.

Tor reads the service key from ``hs_ed25519_secret_key`` in the ``HiddenServiceDir``.
:func:`write_hidden_service_dir` writes that file from the derived seed, with the
public key and the host name beside it.
"""

import base64
import hashlib
import os
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from mnemonic import Mnemonic

_SEED_PERSONALISATION = b"celaut-tor-onion-v3-gateway"

# rend-spec-v3 section 6 and tor's key file format (src/lib/crypt_ops/crypto_format.c).
_SECRET_HEADER = b"== ed25519v1-secret: type0 ==\x00\x00\x00"
_PUBLIC_HEADER = b"== ed25519v1-public: type0 ==\x00\x00\x00"
_CHECKSUM_PREFIX = b".onion checksum"
_VERSION = b"\x03"


def derive_seed(mnemonic: str) -> bytes:
    """The 32-byte ed25519 seed of the gateway's onion service."""
    mnemo = Mnemonic("english")
    if not mnemo.check(mnemonic):
        raise ValueError("Invalid mnemonic phrase.")
    digest = hashes.Hash(hashes.BLAKE2b(64))
    digest.update(_SEED_PERSONALISATION + mnemo.to_seed(mnemonic, passphrase=""))
    return digest.finalize()[:32]


def expanded_secret_key(seed: bytes) -> bytes:
    """The 64-byte secret key tor stores: the clamped scalar, then the nonce prefix."""
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h)


def public_key(seed: bytes) -> bytes:
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def onion_host(public: bytes) -> str:
    """The v3 ``.onion`` name of an ed25519 public key."""
    checksum = hashlib.sha3_256(_CHECKSUM_PREFIX + public + _VERSION).digest()[:2]
    return base64.b32encode(public + checksum + _VERSION).decode().lower() + ".onion"


def host_from_mnemonic(mnemonic: str) -> str:
    return onion_host(public_key(derive_seed(mnemonic)))


def write_hidden_service_dir(directory: Path, seed: bytes) -> Tuple[str, Path]:
    """Write the three files tor reads from a ``HiddenServiceDir``. Returns ``(host, dir)``.

    The directory must be mode 0700 and the files private, or tor refuses to start.
    """
    public = public_key(seed)
    host = onion_host(public)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    files = {
        "hs_ed25519_secret_key": _SECRET_HEADER + expanded_secret_key(seed),
        "hs_ed25519_public_key": _PUBLIC_HEADER + public,
        "hostname": (host + "\n").encode(),
    }
    for name, content in files.items():
        path = directory / name
        path.write_bytes(content)
        os.chmod(path, 0o600)
    return host, directory
