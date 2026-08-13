"""Ergo's Schnorr signature over secp256k1, in pure Python.

This is the signature scheme Ergo itself uses for ``proveDlog`` (P2PK) proofs, and the
one ChainCash/Basis reuse off-chain for their IOU notes. Nodo signs with it so that a
signature it produces is the *same kind of object* the rest of the Ergo ecosystem
produces -- verifiable by ErgoScript, by the Scala reference implementation
(``basis-tracker``'s ``SigUtils``), and by chaincash-rs -- rather than an ad-hoc scheme
that merely happens to use the same key.

Wire format, 65 bytes::

    signature = a (33-byte SEC-compressed point) || z (32-byte big-endian scalar)

Signing, given message ``m``, private scalar ``s`` and public key ``P = s*G``::

    k <- random in [1, n)
    a = compress(k*G)
    e = blake2b256(a || m || P)
    z = (k + e*s) mod n

Verification is the group identity ``z*G == A + e*P``.

Two conventions matter for interoperability, and they are the whole reason this module
exists instead of a one-line call into a library:

* **``e`` is read as a *signed* big-endian integer**, because that is what the on-chain
  verifier does: the reserve contract computes ``byteArrayToBigInt(e)`` and checks
  ``g.exp(z) == a.multiply(pk.exp(e))`` (``basis.es``, redemption branch), and ErgoScript
  reads a 32-byte value as two's-complement -- so a digest whose top byte is >= 0x80
  denotes a *negative* challenge. Reading it as unsigned computes a different ``e mod n``
  (the two differ by 2**256, which is not a multiple of ``n``), which means the two
  conventions disagree about which signatures are valid. Nodo follows the contract, so a
  signature accepted here is one that can actually be redeemed. Note that basis-tracker's
  Rust verifier reads ``e`` as *unsigned* and therefore accepts negative-challenge
  signatures that the contract would reject (see ``tests/test_ergo_schnorr.py``).
* **When signing, both ``e`` and ``z`` are forced to have their top byte < 0x80**, retrying
  the nonce otherwise. For ``z`` this is required: it is serialised raw, and a top bit set
  would be read on-chain as a negative scalar. For ``e`` it is not required by Scala, but
  constraining it makes the signed and unsigned readings *identical*, so signatures Nodo
  emits verify under either convention -- including chaincash-rs / basis-tracker's Rust
  verifier, which reads ``e`` as unsigned.

Only ``ecdsa`` (already a dependency, used for the BIP-32 identity keypair) and ``hashlib``
are needed: no JVM, no Ergo node. Same reason ``bip_wallet_verification`` derives keys in
pure Python -- a node must be able to sign from first boot.
"""
from __future__ import annotations

import hashlib
from secrets import randbelow
from typing import Union

import ecdsa

CURVE = ecdsa.SECP256k1
GENERATOR = CURVE.generator
ORDER: int = CURVE.order

# Ergo hashes with Blake2b-256 throughout (script hashes, box ids, the sigma-protocol
# Fiat-Shamir challenge). Taken from hashlib directly rather than from
# ``src.utils.hashing``: that module is about content-addressing and pulls in ConfigManager.
BLAKE2B_DIGEST_SIZE = 32

# a is a compressed point, z a 32-byte scalar.
_A_LENGTH = 33
_Z_LENGTH = 32
SIGNATURE_LENGTH = _A_LENGTH + _Z_LENGTH
PUBLIC_KEY_LENGTH = 33

# A 32-byte big-endian value is negative in two's complement -- and so rejected on-chain
# by `byteArrayToBigInt` -- exactly when its most significant byte has the top bit set.
_TOP_BIT = 0x80


def blake2b256(data: bytes) -> bytes:
    """Ergo's hash: Blake2b truncated to a 32-byte digest."""
    return hashlib.blake2b(data, digest_size=BLAKE2B_DIGEST_SIZE).digest()


def _decode_point(compressed: bytes):
    """The curve point for a 33-byte SEC-compressed encoding.

    Raises for anything that is not a valid point, including the wrong length and a
    prefix other than 0x02/0x03; callers turn that into a rejected signature.
    """
    if len(compressed) != PUBLIC_KEY_LENGTH or compressed[0] not in (0x02, 0x03):
        raise ValueError("Not a SEC-compressed secp256k1 point.")
    return ecdsa.VerifyingKey.from_string(
        compressed, curve=CURVE, valid_encodings=["compressed"]
    ).pubkey.point


def compress_point(point) -> bytes:
    """The 33-byte SEC-compressed encoding of a curve point."""
    return ecdsa.VerifyingKey.from_public_point(point, curve=CURVE).to_string("compressed")


def challenge(a: bytes, message: bytes, public_key: bytes) -> int:
    """``e`` as the reference implementation computes it: signed big-endian Blake2b256.

    Signed, because that is what Scala's ``BigInt(Array[Byte])`` and ErgoScript's
    ``byteArrayToBigInt`` do with the same bytes. See the module docstring.
    """
    return int.from_bytes(blake2b256(a + message + public_key), "big", signed=True)


def sign(message: bytes, private_key: Union[int, bytes], public_key: bytes) -> bytes:
    """Sign ``message``, returning the 65-byte ``a || z`` signature.

    ``public_key`` is this key's own 33-byte compressed public key: it is part of the
    challenge, so it is passed in rather than re-derived on every call (deriving it costs
    a scalar multiplication, and the caller always has it already).

    The nonce is redrawn until both ``e`` and ``z`` encode as positive 32-byte values --
    see the module docstring for why. Each retry has probability ~1/4, so the loop is not
    a practical concern, but it is unbounded on purpose: a bounded loop would have to
    either emit a signature that fails on-chain or raise, and both are worse than looping.
    """
    if isinstance(private_key, bytes):
        private_key = int.from_bytes(private_key, "big")
    if not 0 < private_key < ORDER:
        raise ValueError("Private key out of range for secp256k1.")
    if len(public_key) != PUBLIC_KEY_LENGTH:
        raise ValueError("Public key must be a 33-byte SEC-compressed point.")

    while True:
        nonce = randbelow(ORDER - 1) + 1  # in [1, ORDER)
        a = compress_point(GENERATOR * nonce)

        e_bytes = blake2b256(a + message + public_key)
        if e_bytes[0] & _TOP_BIT:
            # Would be a negative challenge under the signed reading; keeping it positive
            # makes this signature verify under both conventions.
            continue
        e = int.from_bytes(e_bytes, "big")

        z = (nonce + e * private_key) % ORDER
        if z == 0 or z.bit_length() > 255:
            # z.bitLength <= 255 is the reference implementation's own retry condition.
            continue

        return a + z.to_bytes(_Z_LENGTH, "big")


def verify(message: bytes, public_key: bytes, signature: bytes) -> bool:
    """Whether ``signature`` over ``message`` was produced by ``public_key``.

    Returns False for every malformed input rather than raising: this runs on data
    received from the network (``GetPeerInfo``, IOU notes), where a bad signature and a
    bad encoding are the same answer to the caller.
    """
    if len(signature) != SIGNATURE_LENGTH or len(public_key) != PUBLIC_KEY_LENGTH:
        return False

    a_bytes, z_bytes = signature[:_A_LENGTH], signature[_A_LENGTH:]
    try:
        a_point = _decode_point(a_bytes)
        public_point = _decode_point(public_key)
    except (ValueError, ecdsa.MalformedPointError):
        return False

    z = int.from_bytes(z_bytes, "big")
    if not 0 < z < ORDER:
        return False

    e = challenge(a_bytes, message, public_key) % ORDER
    return GENERATOR * z == a_point + public_point * e
