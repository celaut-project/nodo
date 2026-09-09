"""The donation configuration, per ledger and per payment method.

Two wallet lists, and they are not interchangeable:

``DONATION_WALLETS``
    Who this node funds. Paying costs money, so the list is short and it is a
    prospective bet -- on a developer being recognised by other nodes later.

``DONATION_CREDIT_WALLETS``
    Whose contributions this node recognises when it routes work. Holding an opinion
    is free, so this list is long and accumulates. It weighs *other* peers' donations,
    which is what makes donating worth anything, and it is the more security-sensitive
    of the two: a bad pay list costs the operator who set it, a bad count list is paid
    for by everyone this node routes to.

Both live under their ledger, because an address only means anything on its own chain.
Only the formula's parameters are global, and those live under ``balancers:``.

Money-shaped settings are per payment *method* -- ``(ledger, contract, asset)``. A debt
accrued in one asset is not a debt in another, so the percentage and the minimum
transfer are read for the asset that was actually paid. The wallet lists are the
exception and stay per ledger: an address receives whatever is sent to it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from src.utils.config import ConfigManager

# The reserved symbol every ledger names its own native unit by ("ERG", "BTC"). The
# native unit's donation settings live directly in the ledger's `payments:` block; a
# token settling through the same contract declares its own inside its `ASSETS:` entry.
# A 64-hex token id can never collide with a symbol like this.
PERCENTAGE_KEY = "DONATION_PERCENTAGE"
MIN_TRANSFER_KEY = "DONATION_MIN_TRANSFER"
PAY_WALLETS_KEY = "DONATION_WALLETS"
CREDIT_WALLETS_KEY = "DONATION_CREDIT_WALLETS"
MIN_CONFIRMATIONS_KEY = "DONATION_MIN_CONFIRMATIONS"

DEFAULT_MIN_CONFIRMATIONS = 10


@dataclass(frozen=True)
class Wallet:
    """One entry of a wallet list: where, and how much of the list it accounts for."""

    address: str
    weight: Decimal


def _payments_block(ledger: str) -> Dict[str, Any]:
    block = ConfigManager().get(f"ledgers.{ledger}.payments")
    return block if isinstance(block, dict) else {}


def _asset_block(ledger: str, asset: str) -> Dict[str, Any]:
    """The config block that owns ``asset``'s donation settings on ``ledger``.

    The ledger's ``payments:`` block for its native unit; the matching ``ASSETS:``
    entry for a token. Falls back to the ledger block, so a ledger that settles in one
    asset -- every ledger, today -- needs no ``ASSETS`` list at all and reads exactly
    the keys it declares.
    """
    payments = _payments_block(ledger)
    assets = payments.get("ASSETS")
    if isinstance(assets, list):
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("TOKEN_ID") or "").strip() == asset:
                return entry
    return payments


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def percentage(ledger: str, asset: str) -> Decimal:
    """Share of an incoming payment on this method that is owed as a donation.

    Clamped to [0, 1] rather than rejected: this is read on the payment path, where the
    money has already arrived, and refusing to credit a client because a percentage is
    malformed would be a worse outcome than donating nothing. Startup validation is
    where a bad value is reported (``utils.config_validation``).
    """
    raw = _asset_block(ledger, asset).get(PERCENTAGE_KEY)
    share = _decimal(raw if raw not in (None, "") else 0)
    if share is None:
        return Decimal(0)
    return max(Decimal(0), min(share, Decimal(1)))


def min_transfer(ledger: str, asset: str) -> Decimal:
    """Smallest payout worth making, in **whole units** of the asset.

    Whole units, not the native base unit, because that is the shape every other
    monetary setting in the config uses (``HOT_WALLET_LIMITS`` and friends are ERG
    decimal strings). Converting to base units needs the asset's own decimals, so the
    ledger's own interface does it -- this module stays free of any chain's units.
    """
    raw = _asset_block(ledger, asset).get(MIN_TRANSFER_KEY)
    amount = _decimal(raw if raw not in (None, "") else 0)
    if amount is None or amount < 0:
        return Decimal(0)
    return amount


def min_confirmations(ledger: str) -> int:
    """Confirmations a donation needs before it counts. Never reads the mempool."""
    raw = _payments_block(ledger).get(MIN_CONFIRMATIONS_KEY)
    try:
        confirmations = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIRMATIONS
    return max(0, confirmations)


def _wallets(ledger: str, key: str) -> List[Wallet]:
    entries = _payments_block(ledger).get(key)
    if not isinstance(entries, list):
        return []
    wallets: List[Wallet] = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = str(entry.get("address") or "").strip()
        if not address or address in seen:
            # A duplicate address is refused at startup; here it is dropped, because
            # counting one twice would silently double its weight.
            continue
        weight = _decimal(entry.get("weight", 0))
        if weight is None or weight < 0:
            continue
        seen.add(address)
        wallets.append(Wallet(address=address, weight=weight))
    return wallets


def pay_wallets(ledger: str) -> List[Wallet]:
    """Who this node funds on ``ledger``, in the order the config declares them."""
    return _wallets(ledger, PAY_WALLETS_KEY)


def credit_wallets(ledger: str) -> List[Wallet]:
    """Whose donations this node counts on ``ledger``."""
    return _wallets(ledger, CREDIT_WALLETS_KEY)


def credit_weights(ledger: str) -> Dict[str, Decimal]:
    """Counted address -> its weight, normalised to sum 1 within the ledger.

    Normalisation is not cosmetic. Unnormalised weights of 100 would multiply every
    credit by 100, saturate every peer at the top of the bonus curve, and turn the
    donation term into a constant that discriminates nothing.
    """
    from src.payment_system.donations.split import normalised

    return {wallet.address: wallet.weight for wallet in normalised(credit_wallets(ledger))}
