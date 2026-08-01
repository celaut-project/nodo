"""
Pure-Python Ergo monetary and address helpers.

Monetary configuration (hot-wallet limit, cold-wallet minimum transfer) is written
as a decimal string in ERG. It is parsed exactly ONCE with :class:`decimal.Decimal`
and converted to an integer amount of nanoERG here; every subsequent arithmetic step
uses integers, never floats. This avoids the floating-point drift the old ERG-based
sweep logic suffered from.

Ergo address validation is done structurally (base58 + blake2b checksum) so the config
can be validated without booting a JVM / AppKit.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import blake2b
from typing import Union

NANOERG_PER_ERG = 1_000_000_000

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

# Ergo address-header network/type prefixes (first byte of the decoded payload).
# P2PK=0x01, P2SH=0x02, P2S=0x03 for mainnet; testnet adds 0x10.
_MAINNET_PREFIXES = {0x01, 0x02, 0x03}
_TESTNET_PREFIXES = {0x11, 0x12, 0x13}


def erg_to_nanoerg(value: Union[str, int, Decimal]) -> int:
    """
    Convert an ERG amount (decimal string / int / Decimal) to integer nanoERG.

    Raises ``ValueError`` for non-numeric input, negative amounts, or amounts with
    sub-nanoERG precision (more than 9 decimal places) that cannot be represented
    exactly as an integer number of nanoERG.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly.
        raise ValueError(f"Invalid ERG amount: {value!r}")
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid ERG amount: {value!r}") from exc

    if dec.is_nan() or dec.is_infinite():
        raise ValueError(f"Invalid ERG amount: {value!r}")
    if dec < 0:
        raise ValueError(f"ERG amount must not be negative: {value!r}")

    nano = dec * NANOERG_PER_ERG
    if nano != nano.to_integral_value():
        raise ValueError(
            f"ERG amount {value!r} is not representable in whole nanoERG "
            "(max 9 decimal places)."
        )
    return int(nano)


def nanoerg_to_erg_str(nano: int) -> str:
    """Human-readable ERG string for a nanoERG integer (display/logging only)."""
    erg = Decimal(int(nano)) / NANOERG_PER_ERG
    # Fixed-point, no scientific notation; trim trailing zeros but keep at least "0".
    text = format(erg, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _b58decode(data: str) -> bytes:
    num = 0
    for char in data:
        if char not in _B58_INDEX:
            raise ValueError(f"Invalid base58 character: {char!r}")
        num = num * 58 + _B58_INDEX[char]
    # Big-endian bytes.
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Restore leading zero bytes (encoded as '1').
    pad = len(data) - len(data.lstrip("1"))
    return b"\x00" * pad + body


def is_valid_ergo_address(address: str, network: str = "mainnet") -> bool:
    """
    Structurally validate an Ergo address: base58 decodes, the header byte matches the
    requested network/type, and the trailing 4-byte blake2b256 checksum verifies.

    No JVM required. Returns ``False`` for any malformed input.
    """
    if not address or not isinstance(address, str):
        return False
    try:
        raw = _b58decode(address)
    except ValueError:
        return False
    if len(raw) < 5:
        return False
    payload, checksum = raw[:-4], raw[-4:]
    prefix = payload[0]
    allowed = _MAINNET_PREFIXES if network == "mainnet" else _TESTNET_PREFIXES
    if prefix not in allowed:
        return False
    calculated = blake2b(payload, digest_size=32).digest()[:4]
    return calculated == checksum
