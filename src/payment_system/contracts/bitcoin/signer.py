"""Signing Bitcoin transactions locally, from the mnemonic the node holds.

The same posture Ergo has: a key in ``config.yaml``, a public node for everything that
is a question about the chain. A public API cannot sign for you, but it does not have to
-- it only has to say which outputs the wallet owns and carry the transaction once it is
built. Everything between those two is arithmetic, and it is here.

Deliberately one thing and no more: a single **P2WPKH** key at ``m/84'/0'/0'/0/0``
(``m/84'/1'/0'/0/0`` off mainnet), BIP-84, the ordinary path. The same words open the
same funds in any standard wallet, and in the ``service`` backend, whose bitcoind derives
its account from the same mnemonic at the same path. One address, because the contract
advertises one fixed ``scriptPubKey`` (see ``interface.py``).

**What is trusted, and what is not.** The explorer is trusted to report the wallet's
outputs and to relay the transaction. It cannot spend anything: BIP-143 commits the
signature to each input's amount, so an output it misreports yields a signature the
network rejects, not a transaction that loses money. The worst a lying explorer does is
make a payment fail, or hide a balance.

**The key never leaves this module.** :class:`WalletKey` keeps its private half out of
its ``repr``, and nothing here logs or raises with the mnemonic, the seed or a key in the
message.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from functools import lru_cache
from math import ceil
from typing import List, Optional, Sequence, Tuple

import ecdsa
from ecdsa.util import sigdecode_der, sigencode_der_canonize
from mnemonic import Mnemonic

from src.utils.bitcoin_units import P2WPKH_DUST_SAT, address_from_script_pubkey

HARDENED = 0x80000000
_ORDER = ecdsa.SECP256k1.order

# BIP-84: purpose' / coin type' / account' / change / index. Coin type 0 is mainnet and
# 1 is every test network, as the BIP says.
_PURPOSE = 84
_COIN_TYPE = {"mainnet": 0, "testnet": 1, "signet": 1, "regtest": 1}

TX_VERSION = 2
# Opts in to replacement, as Core does by default. Nothing here depends on it: the payer
# waits for confirmations before saying a word to the peer.
SEQUENCE = 0xFFFFFFFD
LOCKTIME = 0
SIGHASH_ALL = 1

# The relay floor. A rate below it is a transaction no node carries.
MIN_FEE_RATE_SAT_VB = 1.0
# The largest data push standard relay policy accepts in an OP_RETURN output.
MAX_OP_RETURN_BYTES = 80
# Dust for the outputs this builds that are not P2WPKH (legacy P2PKH is the largest).
LEGACY_DUST_SAT = 546

# One P2WPKH input's witness at its largest: the item count, a DER signature plus its
# sighash byte (72 at most), and the compressed key. Sizing for the largest keeps the fee
# at or above the rate asked for; the signature that comes out is sometimes a byte short.
_WITNESS_BYTES_PER_INPUT = 1 + (1 + 72) + (1 + 33)
_INPUT_BYTES = 32 + 4 + 1 + 4  # outpoint, empty scriptSig, sequence


class InsufficientFunds(ValueError):
    """The wallet's confirmed outputs cannot cover the payment and its fee."""


@dataclass(frozen=True)
class Utxo:
    txid: str
    vout: int
    value: int  # satoshi


@dataclass(frozen=True)
class SignedTransaction:
    hex: str
    txid: str
    fee: int  # satoshi actually left to the miner, change below dust included


@dataclass(frozen=True)
class WalletKey:
    """The one key this node spends from. Its private half is never printed."""

    private_key: bytes = field(repr=False)
    public_key: bytes
    network: str

    @property
    def pubkey_hash(self) -> bytes:
        return hash160(self.public_key)

    @property
    def script_pubkey(self) -> bytes:
        return b"\x00\x14" + self.pubkey_hash

    @property
    def address(self) -> str:
        address = address_from_script_pubkey(self.script_pubkey, network=self.network)
        if address is None:  # unreachable for a network `derive_wallet_key` accepted
            raise ValueError(f"no address for network {self.network!r}")
        return address


# ------------------------------------------------------------------------ hashing
def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _ripemd160(data: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", data).digest()
    except ValueError:
        # An OpenSSL built without it. The BIP-32 package the node already depends on
        # carries a pure-Python one for exactly this.
        from bip32 import ripemd160

        return ripemd160.ripemd160(data)


def hash160(data: bytes) -> bytes:
    return _ripemd160(hashlib.sha256(data).digest())


# ------------------------------------------------------------------ key derivation
def _public_key(private_key: bytes) -> bytes:
    signing = ecdsa.SigningKey.from_string(private_key, curve=ecdsa.SECP256k1)
    return signing.get_verifying_key().to_string("compressed")


def _child(private_key: bytes, chain_code: bytes, index: int) -> Tuple[bytes, bytes]:
    """BIP-32 CKDpriv."""
    if index & HARDENED:
        data = b"\x00" + private_key + index.to_bytes(4, "big")
    else:
        data = _public_key(private_key) + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    tweak = int.from_bytes(digest[:32], "big")
    child = (tweak + int.from_bytes(private_key, "big")) % _ORDER
    if tweak >= _ORDER or child == 0:
        # Probability about 2^-127. BIP-32 says to move on to the next index, which
        # would silently change which wallet this is; refusing is the honest answer.
        raise ValueError("BIP-32 derivation hit an invalid key")
    return child.to_bytes(32, "big"), digest[32:]


@lru_cache(maxsize=4)
def derive_wallet_key(mnemonic: str, passphrase: str = "",
                      network: str = "mainnet") -> WalletKey:
    """The BIP-84 key at index 0 for ``mnemonic``, or ``ValueError``.

    Cached: the seed costs a 2048-round PBKDF2, and this is asked for on every
    advertisement and every payment.
    """
    if network not in _COIN_TYPE:
        raise ValueError(f"unknown Bitcoin network {network!r}")
    phrase = " ".join(str(mnemonic).split())
    # Never echoes the words: this error reaches logs and `nodo doctor`.
    if not Mnemonic("english").check(phrase):
        raise ValueError("the mnemonic is not a valid BIP-39 phrase")

    seed = Mnemonic.to_seed(phrase, passphrase=passphrase or "")
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    if not 0 < int.from_bytes(key, "big") < _ORDER:
        raise ValueError("BIP-32 derivation hit an invalid key")
    path = (
        _PURPOSE | HARDENED,
        _COIN_TYPE[network] | HARDENED,
        0 | HARDENED,
        0,  # external chain
        0,  # the one address
    )
    for index in path:
        key, chain = _child(key, chain, index)
    return WalletKey(private_key=key, public_key=_public_key(key), network=network)


# ----------------------------------------------------------------- serialisation
def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _output(script: bytes, value: int) -> bytes:
    return value.to_bytes(8, "little") + _varint(len(script)) + script


def op_return_script(data: bytes) -> bytes:
    """``OP_RETURN <data>``, as a standard output script."""
    if len(data) > MAX_OP_RETURN_BYTES:
        raise ValueError(
            f"an OP_RETURN carries at most {MAX_OP_RETURN_BYTES} bytes, got {len(data)}"
        )
    if len(data) <= 75:
        return b"\x6a" + bytes([len(data)]) + data
    return b"\x6a\x4c" + bytes([len(data)]) + data


def _outpoint(utxo: Utxo) -> bytes:
    return bytes.fromhex(utxo.txid)[::-1] + utxo.vout.to_bytes(4, "little")


def _vsize(inputs: int, scripts: Sequence[bytes]) -> int:
    """Virtual size of a transaction with ``inputs`` P2WPKH inputs and these outputs."""
    base = (
        4 + len(_varint(inputs)) + inputs * _INPUT_BYTES
        + len(_varint(len(scripts)))
        + sum(8 + len(_varint(len(s))) + len(s) for s in scripts)
        + 4
    )
    witness = 2 + inputs * _WITNESS_BYTES_PER_INPUT  # marker and flag, then the items
    return ceil((base * 4 + witness) / 4)


def _dust(script: bytes) -> int:
    is_p2wpkh = len(script) == 22 and script[:2] == b"\x00\x14"
    return P2WPKH_DUST_SAT if is_p2wpkh else LEGACY_DUST_SAT


# ---------------------------------------------------------------------- the build
def build_transaction(
    key: WalletKey,
    utxos: Sequence[Utxo],
    outputs: Sequence[Tuple[bytes, int]],
    *,
    fee_rate: float,
    op_return: Optional[bytes] = None,
    subtract_fee_from: Optional[Sequence[int]] = None,
) -> SignedTransaction:
    """Spend ``utxos`` (all of them this wallet's) to ``outputs``, and sign it.

    ``outputs`` are ``(scriptPubKey, satoshi)`` pairs, in the order they will have in the
    transaction. The ``OP_RETURN``, when asked for, follows them and change comes last,
    so the positions the caller chose are the positions they get.

    ``subtract_fee_from`` names output positions that pay the fee instead of the wallet.
    A sweep needs it: it moves most of a balance, so it cannot know in advance how many
    outputs it will have to spend, and an output that shrinks by the difference is a
    transaction that always funds where an input shortfall would not.

    Inputs are chosen largest first and only as many as the amount and the fee need. Change
    below dust is not made; it goes to the miner and is counted in ``fee``.
    """
    if not outputs:
        raise ValueError("nothing to send: no outputs")
    if any(not isinstance(value, int) or value <= 0 for _, value in outputs):
        raise ValueError("every output must be a positive whole number of satoshi")
    if not fee_rate > 0:
        raise ValueError("the fee rate must be positive")
    rate = max(float(fee_rate), MIN_FEE_RATE_SAT_VB)
    subtract = sorted(set(subtract_fee_from or ()))
    if any(not 0 <= i < len(outputs) for i in subtract):
        raise ValueError("subtract_fee_from names an output that does not exist")

    scripts = [script for script, _ in outputs]
    if op_return is not None:
        scripts.append(op_return_script(op_return))
    change_script = key.script_pubkey
    paid = sum(value for _, value in outputs)

    selected: List[Utxo] = []
    total = 0
    fee = 0
    for utxo in sorted(utxos, key=lambda u: u.value, reverse=True):
        if utxo.value <= 0:
            continue
        selected.append(utxo)
        total += utxo.value
        # Sized with change: dropping it later only makes the transaction smaller.
        fee = ceil(rate * _vsize(len(selected), scripts + [change_script]))
        if total >= (paid if subtract else paid + fee):
            break
    else:
        raise InsufficientFunds(
            f"the wallet holds {total} sat in confirmed outputs, and this needs "
            f"{paid if subtract else paid + fee} sat including a fee of {fee} sat"
        )

    values = [value for _, value in outputs]
    if subtract:
        share, extra = divmod(fee, len(subtract))
        for n, position in enumerate(subtract):
            values[position] -= share + (extra if n == 0 else 0)
        if any(value < _dust(scripts[i]) for i, value in enumerate(values)):
            raise InsufficientFunds(
                f"a fee of {fee} sat leaves an output below the network's dust limit"
            )

    final_outputs = [(scripts[i], values[i]) for i in range(len(outputs))]
    if op_return is not None:
        final_outputs.append((scripts[len(outputs)], 0))
    change = total - sum(values) - fee
    if change >= _dust(change_script):
        final_outputs.append((change_script, change))
    else:
        fee = total - sum(values)

    return _sign(key, selected, final_outputs, fee)


def _sign(key: WalletKey, inputs: Sequence[Utxo],
          outputs: Sequence[Tuple[bytes, int]], fee: int) -> SignedTransaction:
    serialised_outputs = b"".join(_output(script, value) for script, value in outputs)
    version = TX_VERSION.to_bytes(4, "little")
    locktime = LOCKTIME.to_bytes(4, "little")
    sequence = SEQUENCE.to_bytes(4, "little")

    hash_prevouts = sha256d(b"".join(_outpoint(u) for u in inputs))
    hash_sequence = sha256d(sequence * len(inputs))
    hash_outputs = sha256d(serialised_outputs)
    script_code = b"\x19\x76\xa9\x14" + key.pubkey_hash + b"\x88\xac"

    signing = ecdsa.SigningKey.from_string(key.private_key, curve=ecdsa.SECP256k1)
    verifying = signing.get_verifying_key()

    witnesses = []
    for utxo in inputs:
        # BIP-143. The amount is inside what is signed, which is why a lying explorer
        # cannot make this sign away more than the real output holds.
        digest = sha256d(
            version + hash_prevouts + hash_sequence + _outpoint(utxo) + script_code
            + utxo.value.to_bytes(8, "little") + sequence + hash_outputs + locktime
            + SIGHASH_ALL.to_bytes(4, "little")
        )
        der = signing.sign_digest_deterministic(
            digest, hashfunc=hashlib.sha256, sigencode=sigencode_der_canonize
        )
        # A signature that does not verify is a transaction that cannot confirm, found
        # here instead of after a deposit token has been spent on it.
        if not verifying.verify_digest(der, digest, sigdecode=sigdecode_der):
            raise ValueError("the signature did not verify; nothing was built")
        signature = der + bytes([SIGHASH_ALL])
        witnesses.append(
            b"\x02" + _varint(len(signature)) + signature
            + _varint(len(key.public_key)) + key.public_key
        )

    serialised_inputs = b"".join(_outpoint(u) + b"\x00" + sequence for u in inputs)
    body = (
        version + _varint(len(inputs)) + serialised_inputs
        + _varint(len(outputs)) + serialised_outputs
    )
    txid = sha256d(body + locktime)[::-1].hex()
    raw = version + b"\x00\x01" + body[4:] + b"".join(witnesses) + locktime
    return SignedTransaction(hex=raw.hex(), txid=txid, fee=fee)
