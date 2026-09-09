"""
Pure-Python Bitcoin monetary and address helpers.

The mirror of ``src/utils/ergo_units.py``, and for the same two reasons. Monetary
configuration (hot-wallet limit, cold-wallet minimum transfer, minimum donation) is
written as a decimal string in BTC, parsed exactly ONCE with :class:`decimal.Decimal`
into integer satoshi; every subsequent arithmetic step is integers, never floats.

Address validation is structural -- bech32/bech32m for segwit, base58check for legacy --
so the config can be validated without a Bitcoin node. That matters more here than it
did for Ergo: a cold wallet is where an operator's savings go, and checking it by asking
`bitcoind` would mean a node that cannot reach its RPC accepts a typo silently and
sweeps to nowhere.

BTC <-> satoshi (1e8) is not configurable: it is fixed by the protocol, and making it a
setting would only allow defining a wrong Bitcoin.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Optional, Tuple, Union

SATOSHI_PER_BTC = 100_000_000

# The dust threshold for a P2WPKH output, in satoshi: the point below which Bitcoin Core
# will not relay a transaction creating it. Not configurable, and not a policy this node
# chooses -- it is what the network will carry.
P2WPKH_DUST_SAT = 294

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

# Base58check version bytes: P2PKH and P2SH, per network.
_B58_VERSIONS = {
    "mainnet": {0x00, 0x05},
    "testnet": {0x6F, 0xC4},
    "signet": {0x6F, 0xC4},
    "regtest": {0x6F, 0xC4},
}

# Human-readable part of a segwit address, per network (BIP-173, BIP-350).
_BECH32_HRP = {
    "mainnet": "bc",
    "testnet": "tb",
    "signet": "tb",
    "regtest": "bcrt",
}

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def btc_to_satoshi(value: Union[str, int, Decimal]) -> int:
    """
    Convert a BTC amount (decimal string / int / Decimal) to integer satoshi.

    Raises ``ValueError`` for non-numeric input, negative amounts, or amounts with
    sub-satoshi precision (more than 8 decimal places) that cannot be represented
    exactly as an integer number of satoshi.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly.
        raise ValueError(f"Invalid BTC amount: {value!r}")
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid BTC amount: {value!r}") from exc

    if dec.is_nan() or dec.is_infinite():
        raise ValueError(f"Invalid BTC amount: {value!r}")
    if dec < 0:
        raise ValueError(f"BTC amount must not be negative: {value!r}")

    sat = dec * SATOSHI_PER_BTC
    if sat != sat.to_integral_value():
        raise ValueError(
            f"BTC amount {value!r} is not representable in whole satoshi "
            "(max 8 decimal places)."
        )
    return int(sat)


def satoshi_to_btc_str(sat: int) -> str:
    """Human-readable BTC string for a satoshi integer (display/logging only)."""
    btc = Decimal(int(sat)) / SATOSHI_PER_BTC
    text = format(btc, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _b58decode(data: str) -> bytes:
    num = 0
    for char in data:
        if char not in _B58_INDEX:
            raise ValueError(f"Invalid base58 character: {char!r}")
        num = num * 58 + _B58_INDEX[char]
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(data) - len(data.lstrip("1"))
    return b"\x00" * pad + body


def _bech32_polymod(values) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i, bit in enumerate(generator):
            if (top >> i) & 1:
                checksum ^= bit
    return checksum


def _bech32_hrp_expand(hrp: str):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_decode(address: str) -> Optional[Tuple[str, list, int]]:
    """``(hrp, data, checksum constant)``, or None when it is not a valid bech32 string.

    The constant says which encoding verified: bech32 (BIP-173, witness v0) or bech32m
    (BIP-350, witness v1+). Which one is required depends on the witness version, and
    getting that pairing wrong is how a v1 address gets accepted under v0's checksum.
    """
    if any(ord(c) < 33 or ord(c) > 126 for c in address):
        return None
    if address.lower() != address and address.upper() != address:
        # Mixed case is invalid: the checksum is defined over one case only.
        return None
    address = address.lower()
    position = address.rfind("1")
    if position < 1 or position + 7 > len(address) or len(address) > 90:
        return None
    hrp = address[:position]
    try:
        data = [_BECH32_CHARSET.index(c) for c in address[position + 1:]]
    except ValueError:
        return None
    verified = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if verified not in (_BECH32_CONST, _BECH32M_CONST):
        return None
    return hrp, data[:-6], verified


def _convertbits(data, frombits: int, tobits: int, pad: bool = True) -> Optional[list]:
    accumulator = 0
    bits = 0
    result = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        accumulator = (accumulator << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            result.append((accumulator >> bits) & maxv)
    if pad:
        if bits:
            result.append((accumulator << (tobits - bits)) & maxv)
    elif bits >= frombits or ((accumulator << (tobits - bits)) & maxv):
        return None
    return result


def _bech32_encode(hrp: str, data, constant: int) -> str:
    values = _bech32_hrp_expand(hrp) + list(data)
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ constant
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in list(data) + checksum)


def script_pubkey_from_address(address: str, network: str = "mainnet") -> Optional[bytes]:
    """The raw ``scriptPubKey`` a segwit ``address`` pays to, or ``None``.

    This is the value a payment contract advertises as its ``script`` xattr, exactly as
    Ergo advertises the wallet's propositionBytes: the bytes a chain matches on, never a
    human-readable address. A peer that receives it can compare it to the outputs of a
    transaction without decoding anything.

    Segwit only. A legacy address has a scriptPubKey too, but this node advertises
    P2WPKH and there is no reason to build the other forms.
    """
    if not is_valid_bitcoin_address(address, network=network):
        return None
    decoded = _bech32_decode(address)
    if decoded is None:
        return None
    _, data, _ = decoded
    program = _convertbits(data[1:], 5, 8, False)
    if program is None:
        return None
    witness_version = data[0]
    # OP_0 is 0x00; OP_1..OP_16 are 0x51..0x60. Then a push of the program's length.
    opcode = 0x00 if witness_version == 0 else 0x50 + witness_version
    return bytes([opcode, len(program)]) + bytes(program)


def address_from_script_pubkey(script: bytes, network: str = "mainnet") -> Optional[str]:
    """The segwit address a raw ``scriptPubKey`` pays to, or ``None``.

    The inverse of :func:`script_pubkey_from_address`, and the reason both exist: a peer
    advertises the script, and Bitcoin Core's ``createrawtransaction`` takes an address.
    Deriving it here keeps that conversion in one pure place instead of asking the node
    to decode a script mid-payment.
    """
    hrp = _BECH32_HRP.get(network)
    if not hrp or not script or len(script) < 4:
        return None
    opcode, length = script[0], script[1]
    program = script[2:]
    if length != len(program):
        return None
    if opcode == 0x00:
        witness_version = 0
    elif 0x51 <= opcode <= 0x60:
        witness_version = opcode - 0x50
    else:
        return None
    if witness_version == 0 and len(program) not in (20, 32):
        return None
    if not 2 <= len(program) <= 40:
        return None
    converted = _convertbits(program, 8, 5)
    if converted is None:
        return None
    constant = _BECH32_CONST if witness_version == 0 else _BECH32M_CONST
    return _bech32_encode(hrp, [witness_version] + converted, constant)


def is_valid_bitcoin_address(address: str, network: str = "mainnet") -> bool:
    """
    Structurally validate a Bitcoin address for ``network``: bech32/bech32m segwit, or
    base58check P2PKH/P2SH. No RPC and no node required.

    Returns ``False`` for any malformed input, and for an address that is valid on a
    *different* network -- a mainnet address configured on a testnet node is not a typo
    the operator wants accepted, it is a sweep to coins nobody can spend.
    """
    if not address or not isinstance(address, str):
        return False

    hrp = _BECH32_HRP.get(network)
    if hrp:
        decoded = _bech32_decode(address)
        if decoded is not None:
            found_hrp, data, constant = decoded
            if found_hrp != hrp or not data:
                return False
            witness_version = data[0]
            program = _convertbits(data[1:], 5, 8, False)
            if program is None or witness_version > 16:
                return False
            if witness_version == 0:
                # BIP-173: v0 is bech32, and only 20 or 32 bytes (P2WPKH / P2WSH).
                return constant == _BECH32_CONST and len(program) in (20, 32)
            # BIP-350: v1+ is bech32m, 2 to 40 bytes.
            return constant == _BECH32M_CONST and 2 <= len(program) <= 40

    versions = _B58_VERSIONS.get(network)
    if not versions:
        return False
    try:
        raw = _b58decode(address)
    except ValueError:
        return False
    if len(raw) != 25:
        return False
    payload, checksum = raw[:-4], raw[-4:]
    if payload[0] not in versions:
        return False
    return sha256(sha256(payload).digest()).digest()[:4] == checksum
