"""What one MU is worth on Bitcoin, and the conversions that follow from it.

The mirror of ``contracts/ergo/rate.py``, and deliberately **light** for the same
reason: ``format_mu`` resolves display units through here and runs on log lines all over
the node, so this must not drag in the payment stack. It imports config and pure
arithmetic, nothing else. ``interface.py`` imports *this*, never the reverse.

**There is no sensible default for the rate, and that is the whole design of this
module.** A satoshi and a nanoERG are about six orders of magnitude apart in value, so
copying Ergo's ``MU_PER_NANOERG: 1`` by analogy would misprice the node by a factor of a
million -- the exact failure `docs/PRICING.md` was written to make impossible, and the
one the gas model actually shipped with. So an unset rate is not filled in: the contract
reports itself unavailable and the node simply does not offer Bitcoin, which is the only
answer that cannot quietly sell an hour of compute for a millionth of its price.

BTC <-> satoshi (1e8) is not configurable and lives in ``src/utils/bitcoin_units.py``:
it is fixed by the protocol, and making it a setting would only allow defining a wrong
Bitcoin.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from src.utils.bitcoin_units import SATOSHI_PER_BTC
from src.utils.config import ConfigManager

RATE_KEY = "ledgers.bitcoin.payments.MU_PER_SATOSHI"

# How this ledger's unit is shown to an operator who picks it as `ui.DISPLAY_UNIT`.
UNIT_NAME = "btc"
UNIT_SYMBOL = "BTC"
UNIT_DECIMALS = 8


def _decimal(value: Any, *, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{what} is not a number: {value!r}") from exc


def rate_reason() -> Optional[str]:
    """Why the rate cannot be used, or ``None`` when it can.

    Separate from :func:`mu_per_satoshi` so the registry can ask without catching:
    "should this contract be offered" is a question with an answer, not an exception.
    """
    raw = ConfigManager().get(RATE_KEY)
    if raw in (None, ""):
        return (
            f"{RATE_KEY} is not set. There is no default: a satoshi and a nanoERG are "
            "about six orders of magnitude apart in value, so a borrowed rate would "
            "misprice this node by a factor of a million. Work it out against your own "
            "market and set it -- see docs/BITCOIN.md."
        )
    try:
        rate = _decimal(raw, what=RATE_KEY)
    except ValueError as exc:
        return str(exc)
    if rate <= 0:
        return f"{RATE_KEY} must be positive, got {rate}."
    if rate * SATOSHI_PER_BTC != (rate * SATOSHI_PER_BTC).to_integral_value():
        return (
            f"{RATE_KEY}={rate} makes one BTC {rate * SATOSHI_PER_BTC} MU, which is not "
            "a whole number of MU. MU is the unit of account; there is nothing smaller."
        )
    return None


def mu_per_satoshi() -> Decimal:
    """How many MU one satoshi buys.

    Read per call, not captured at import: ``ConfigManager`` is a replaceable singleton,
    and a fee-bearing chain's numbers are read on every payment anyway.
    """
    reason = rate_reason()
    if reason:
        raise ValueError(reason)
    return _decimal(ConfigManager().get(RATE_KEY), what=RATE_KEY)


def mu_per_unit() -> int:
    """MU bought by one **whole** BTC. This is what a peer is told as ``ContractRate``."""
    value = mu_per_satoshi() * SATOSHI_PER_BTC
    return int(value)


def mu_to_satoshi(amount_mu: int) -> int:
    """MU -> satoshi, for settling on Bitcoin. Truncates: never claim more than is owed."""
    return int(Decimal(int(amount_mu)) / mu_per_satoshi())


def mu_to_satoshi_exact(amount_mu: int) -> Decimal:
    """MU -> satoshi, keeping the fraction. For a debt, not for a transaction.

    Same split as Ergo's: a transaction truncates so it never claims more than is owed,
    while a donation debt is accrued from many payments and paid once, so truncating
    each conversion would shave a sub-satoshi off every one of them -- always in this
    node's favour.
    """
    return Decimal(int(amount_mu)) / mu_per_satoshi()


def satoshi_to_mu(satoshi: int) -> int:
    """satoshi -> MU, for crediting a payment that arrived."""
    return int(Decimal(int(satoshi)) * mu_per_satoshi())


def display_units() -> Dict[str, Dict[str, Any]]:
    """The display unit this contract contributes, keyed by name.

    Nothing at all when the rate is unset, rather than a unit priced at a guess: an
    operator reading "0.5 BTC" off a node that does not know what a satoshi is worth
    would be reading a made-up number. `format_mu` falls back to raw MU, which is
    honest.
    """
    if rate_reason():
        return {}
    return {
        UNIT_NAME: {
            "SYMBOL": UNIT_SYMBOL,
            "MU_PER_UNIT": Decimal(mu_per_unit()),
            "DECIMALS": UNIT_DECIMALS,
        }
    }
