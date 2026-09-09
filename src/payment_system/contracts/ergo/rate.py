"""What one MU is worth on Ergo, and the conversions that follow from it.

The node counts in MU, its own unit of account (``src/utils/monetary.py``). MU has no
intrinsic value: what one MU is *worth* is a property of each payment contract, and this
module is Ergo's answer. It is what travels to peers as ``ContractRate.mu_per_unit``, and
the single point where this ledger's money meets the node's accounting.

These functions used to live in ``monetary`` itself, which meant the generic money module
named a ledger, read that ledger's config section, and hardcoded its display unit — so
adding a second payment system meant editing the accounting core. Ergo's rate belongs
with Ergo.

Deliberately **light**: it imports config and pure arithmetic, nothing else. ``format_mu``
runs on log lines all over the node and resolves the display unit through here, so this
must not drag in the Ergo payment stack (``interface.py`` pulls in requests, the database
and the reputation system). ``interface.py`` imports *this*, never the reverse.

ERG <-> nanoERG (1e9) is not configurable and lives in ``src/utils/ergo_units.py``: it is
fixed by the Ergo protocol, and making it a setting would only allow defining a wrong Ergo.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

from src.utils.config import ConfigManager
from src.utils.ergo_units import NANOERG_PER_ERG

# Config key holding this ledger's rate. Named here rather than in the accounting core,
# so a second ledger declares its own key without touching shared code.
RATE_KEY = "ledgers.ergo.payments.MU_PER_NANOERG"

# How this ledger's unit is shown to an operator who picks it as `ui.DISPLAY_UNIT`.
UNIT_NAME = "erg"
UNIT_SYMBOL = "ERG"
UNIT_DECIMALS = 9


def _decimal(value: Any, *, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{what} is not a number: {value!r}") from exc


def mu_per_nanoerg() -> Decimal:
    """How many MU one nanoERG buys.

    1 by default: Ergo is the only payment system, so the simplest mapping is the right
    one. It is a setting rather than a definition because the rate belongs to the
    contract — a second ledger settling in another token declares its own — and because
    an operator may want to rescale what an MU means against ERG.

    Read per call, not captured at import: ``ConfigManager`` is a replaceable
    singleton, and tests swap it out under this module.
    """
    raw = ConfigManager().get(RATE_KEY, 1)
    rate = _decimal(raw if raw not in (None, "") else 1, what=RATE_KEY)
    if rate <= 0:
        raise ValueError(f"{RATE_KEY} must be positive, got {rate}.")
    return rate


def mu_per_erg() -> int:
    """MU bought by one whole ERG. This is what a peer is told as ``ContractRate``."""
    rate = mu_per_nanoerg()
    value = rate * NANOERG_PER_ERG
    if value != value.to_integral_value():
        raise ValueError(
            f"{RATE_KEY}={rate} makes one ERG {value} MU, which is not a whole number of MU."
        )
    return int(value)


def mu_to_nanoerg(amount_mu: int) -> int:
    """MU -> nanoERG, for settling on Ergo. Truncates: never claim more than is owed."""
    return int(Decimal(int(amount_mu)) / mu_per_nanoerg())


def mu_to_nanoerg_exact(amount_mu: int) -> Decimal:
    """MU -> nanoERG, keeping the fraction. For a debt, not for a transaction.

    :func:`mu_to_nanoerg` truncates, which is right when the figure is about to become
    an output: never claim more than is owed. A donation debt is the opposite case --
    it is accumulated over many payments and paid once, so truncating each conversion
    would shave a sub-unit off every one of them and always in this node's favour. The
    fraction is kept on the accrual row and paid when it grows into a whole nanoERG.
    """
    return Decimal(int(amount_mu)) / mu_per_nanoerg()


def nanoerg_to_mu(nanoerg: int) -> int:
    """nanoERG -> MU, for crediting a payment that arrived."""
    return int(Decimal(int(nanoerg)) * mu_per_nanoerg())


def display_units() -> Dict[str, Dict[str, Any]]:
    """The display units this contract contributes: ERG plus one per configured asset.

    Same shape as an operator-declared ``ui.UNITS.<name>`` block, so the accounting core
    handles both through one code path and knows nothing about ERG. The difference is that
    this rate is *derived* from the ledger, so it cannot go stale when the rate changes —
    a hand-declared unit is a static number nothing refreshes.

    One entry per asset, and a clash between two of them is refused where the assets are
    read (:func:`assets`) rather than here: this is a ``dict.update``, so the second of
    two assets sharing a ``UNIT_NAME`` would silently win and every figure the operator
    reads would be off by the ratio between the two, with nothing raising.
    """
    units: Dict[str, Dict[str, Any]] = {
        UNIT_NAME: {
            "SYMBOL": UNIT_SYMBOL,
            "MU_PER_UNIT": Decimal(mu_per_erg()),
            "DECIMALS": UNIT_DECIMALS,
        }
    }
    for asset in assets():
        units[asset.unit_name] = {
            "SYMBOL": asset.symbol,
            "MU_PER_UNIT": Decimal(mu_per_whole_unit(asset)),
            "DECIMALS": asset.decimals,
        }
    return units


# Assets other than ERG this node accepts, declared by the operator. Empty by default:
# a node that has not opted in behaves exactly as it did before tokens existed.
ASSETS_KEY = "ledgers.ergo.payments.ASSETS"


@dataclass(frozen=True)
class Asset:
    """One EIP-4 token this node accepts, as the operator declared it.

    Every field comes from config, and that is a decision rather than laziness
    (see #342 §4.3). A token is identified by its 64-hex **id**: anyone can mint a
    token called "SigUSD", so a name is not an identity and this module never resolves
    one, never asks an explorer for "the token called X", and never accepts a box
    because a name matched. ``decimals`` is stated here too, rather than read from the
    minter's EIP-4 registers, because those are self-declared and a wrong one misprices
    the node by a power of ten.
    """

    #: 64 lowercase hex characters. Can never collide with a reserved native symbol
    #: ("ERG"), which is what lets one column hold both.
    token_id: str
    #: How the amount is shown to a person. Presentation only, never an identity.
    symbol: str
    #: Name of the display unit this asset contributes, unique across the node.
    unit_name: str
    decimals: int
    #: MU per **base** unit -- one cent of a 2-decimal token -- mirroring
    #: ``MU_PER_NANOERG`` being MU per base unit of ERG.
    mu_per_base_unit: Decimal


def _hex_token_id(value: Any, *, what: str) -> str:
    raw = str(value or "").strip().lower()
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        raise ValueError(
            f"{what} must be a token's 64-character hex id, got {value!r}. An asset is "
            "its id and never its name: anyone can mint a token called 'SigUSD'."
        )
    return raw


def assets() -> Tuple[Asset, ...]:
    """The configured non-native assets, in the order the operator declared them.

    Order is kept because it is the payer's preference: #340's payment walk tries one
    method and falls through to the next, so a node whose wallet is out of SigUSD but
    holds ERG pays in ERG with no policy and no new setting -- and that has to be
    reproducible rather than set-ordered.

    Read per call, like every other rate here: ``ConfigManager`` is a replaceable
    singleton. Malformed config raises rather than being skipped -- an asset dropped
    because its id was mistyped would leave the node quietly advertising fewer payment
    methods than its operator configured.
    """
    raw = ConfigManager().get(ASSETS_KEY) or []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{ASSETS_KEY} must be a list of assets, got {type(raw).__name__}.")

    parsed: list[Asset] = []
    seen_ids: Dict[str, int] = {}
    seen_units: Dict[str, int] = {}
    for index, entry in enumerate(raw):
        where = f"{ASSETS_KEY}[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a mapping, got {type(entry).__name__}.")

        token_id = _hex_token_id(entry.get("TOKEN_ID"), what=f"{where}.TOKEN_ID")
        if token_id in seen_ids:
            raise ValueError(
                f"{where}.TOKEN_ID repeats the id already declared at "
                f"{ASSETS_KEY}[{seen_ids[token_id]}]. One asset cannot have two rates."
            )

        symbol = str(entry.get("SYMBOL") or "").strip()
        if not symbol:
            raise ValueError(f"{where}.SYMBOL is required: an amount has to say what it is.")

        unit_name = str(entry.get("UNIT_NAME") or "").strip().lower()
        if not unit_name:
            raise ValueError(f"{where}.UNIT_NAME is required (the display unit's name).")
        # A unit name is what `ui.DISPLAY_UNIT` selects and what `format_mu` renders
        # through. Two assets sharing one would make an operator's chosen unit mean
        # whichever asset was declared last, and `display_units` is a `dict.update`, so
        # nothing would raise: every figure on every log line would be off by the ratio
        # between the two.
        if unit_name in seen_units or unit_name == UNIT_NAME:
            clashes_with = (
                f"{ASSETS_KEY}[{seen_units[unit_name]}]" if unit_name in seen_units
                else f"this ledger's own native unit ({UNIT_NAME})"
            )
            raise ValueError(
                f"{where}.UNIT_NAME={unit_name!r} clashes with {clashes_with}. A unit "
                "name has to say which money it is, so rename one."
            )

        decimals_raw = entry.get("DECIMALS", 0)
        try:
            decimals = int(decimals_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{where}.DECIMALS is not a whole number: {decimals_raw!r}") from exc
        if decimals < 0:
            raise ValueError(f"{where}.DECIMALS cannot be negative, got {decimals}.")

        mu_per_base = _decimal(entry.get("MU_PER_UNIT"), what=f"{where}.MU_PER_UNIT")
        if mu_per_base <= 0:
            raise ValueError(f"{where}.MU_PER_UNIT must be positive, got {mu_per_base}.")

        seen_ids[token_id] = index
        seen_units[unit_name] = index
        parsed.append(Asset(
            token_id=token_id, symbol=symbol, unit_name=unit_name,
            decimals=decimals, mu_per_base_unit=mu_per_base,
        ))
    return tuple(parsed)


def asset_for(token_id: str) -> Optional[Asset]:
    """One configured asset by its id, or ``None`` when this node does not accept it."""
    wanted = str(token_id or "").strip().lower()
    for asset in assets():
        if asset.token_id == wanted:
            return asset
    return None


def require_asset(token_id: str) -> Asset:
    """One configured asset by its id, raising when this node does not accept it.

    For the payment path, where the asset was already resolved from a registered method:
    reaching here without a match means the config changed under a payment in flight,
    and settling it against a rate nobody declared is worse than failing.
    """
    asset = asset_for(token_id)
    if asset is None:
        raise ValueError(
            f"No asset with id {token_id} is configured under {ASSETS_KEY}, so there is "
            "no rate to settle it at."
        )
    return asset


def mu_per_whole_unit(asset: Asset) -> int:
    """MU bought by one **whole** unit of ``asset``. What a peer is told as its rate.

    Whole units rather than base ones for the same reason as ``mu_per_erg``: both sides
    convert through the same figure, so the convention only has to be shared, and a
    whole unit is the one a person can check against a price they know.
    """
    value = asset.mu_per_base_unit * (Decimal(10) ** asset.decimals)
    if value != value.to_integral_value():
        raise ValueError(
            f"MU_PER_UNIT={asset.mu_per_base_unit} makes one whole {asset.symbol} "
            f"{value} MU, which is not a whole number of MU."
        )
    return int(value)


def mu_to_base_units(amount_mu: int, asset: Asset) -> int:
    """MU -> base units of ``asset``. Truncates: never claim more than is owed."""
    return int(Decimal(int(amount_mu)) / asset.mu_per_base_unit)


def mu_to_base_units_exact(amount_mu: int, asset: Asset) -> Decimal:
    """MU -> base units of ``asset``, keeping the fraction. For a debt, not a transaction.

    Same split as :func:`mu_to_nanoerg_exact`, and for the same reason: a donation debt
    is accumulated over many payments and paid once, so truncating every conversion would
    shave a sub-unit off every one of them, always in this node's favour.
    """
    return Decimal(int(amount_mu)) / asset.mu_per_base_unit


def base_units_to_mu(base_units: int, asset: Asset) -> int:
    """Base units of ``asset`` -> MU, for crediting a payment that arrived."""
    return int(Decimal(int(base_units)) * asset.mu_per_base_unit)


def base_units_to_str(base_units: int, asset: Asset) -> str:
    """A base-unit amount as a person reads it, at this asset's declared decimals."""
    if asset.decimals <= 0:
        return str(int(base_units))
    scaled = Decimal(int(base_units)) / (Decimal(10) ** asset.decimals)
    return f"{scaled:.{asset.decimals}f}"
