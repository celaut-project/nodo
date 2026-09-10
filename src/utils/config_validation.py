"""
Validation for the single-wallet Ergo configuration.

This is a *breaking* pre-production layout: the old ``reputation`` / ``payments`` root
blocks, the auxiliary/receiver wallet, and the ``PAYMENTS_RECEIVER_WALLET`` key (and its
historical ``PAYMENTS_RECIVER_WALLET`` typo) are gone. A config still carrying any of
those keys is rejected outright — there is no migration or fallback.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from src.utils.arch_guard import CANONICAL_ARCHITECTURES
from src.utils.ergo_units import erg_to_nanoerg, is_valid_ergo_address

# Keys that were removed. Their presence anywhere in the config means the file predates
# a breaking change and must be updated by hand.
REMOVED_KEYS = (
    # Single-wallet refactor.
    "AUXILIARY_MNEMONIC",
    "AUXILIAR_MNEMONIC",
    "PAYMENTS_RECEIVER_WALLET",
    "PAYMENTS_RECIVER_WALLET",
    # ERG-native pricing (docs/PRICING.md). The gas model is gone: prices are now per
    # resource, in ERG, under `pricing:`. Leaving a stale key silently in place would
    # keep a node quoting a price nobody charges, so they are rejected outright.
    "GAS_PER_ERG",
    "EXECUTION_COST",
    "EXECUTION_BENEFIT",
    "BUILD_COST",
    "MODIFY_RESOURCES_COST",
    "FREE_GAS_THRESHOLD",
    "FREE_TRIAL_GAS_AMOUNT",
    "DEFAULT_INITIAL_GAS_AMOUNT",
    "DEFAULT_INITIAL_GAS_AMOUNT_FACTOR",
    "USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR",
    "TOTAL_REFILLED_DEPOSIT",
    "MIN_DEPOSIT_PEER",
    "INITIAL_PEER_DEPOSIT_FACTOR",
    "DEV_CLIENT_GAS_AMOUNT",
    "INIT_COST_CONFIGURATION_FACTOR",
    "MAINTENANCE_COST_CONFIGURATION_FACTOR",
    "TUNNEL_OPEN_COST",
    "TUNNEL_COST_PER_KB",
    "TUNNEL_GAS_CHARGE_INTERVAL_KB",
    "ALLOW_GAS_DEBT",
    "CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME",
    # Prices moved from ERG strings to whole MU, and the ERG rate to
    # ledgers.ergo.payments.MU_PER_NANOERG.
    "RAM_ERG_PER_GIB_HOUR",
    "CPU_ERG_PER_VCPU_HOUR",
    "DISK_ERG_PER_GIB_HOUR",
    "NET_ERG_PER_GIB",
    "BUILD_ERG",
    "TUNNEL_OPEN_ERG",
    "MODIFY_RESOURCES_ERG",
    "CREDIT_ERG_PER_NEW_CLIENT",
    "CLIENT_MIN_BALANCE_ERG_TO_RESET_EXPIRATION",
    # Which architectures the node executes is no longer declared, it is derived
    # from the host arch plus whatever `virtualizers.qemu` can emulate here
    # (src/utils/architectures.py). Keeping these would let a config claim a
    # capability the node does not have -- the exact failure they caused: true on
    # an x86_64 host sent an arm64 service into the CH build, which then died on a
    # guest kernel that was never installed.
    "ARM_SUPPORT",
    "X86_SUPPORT",
    # Donations became a share of *earnings* paid to a weighted list of wallets, so the
    # single wallet key is gone along with the split of the cold-wallet sweep that used
    # it. Leaving it in place would read as configured and donate nothing: the sweep no
    # longer looks at it, and with no cold wallet -- the default -- it never ran at all.
    "DONATION_WALLET",
)


class ConfigValidationError(ValueError):
    """Raised when the Ergo configuration is structurally invalid."""


def _find_removed_keys(obj: Any, path: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            if key in REMOVED_KEYS:
                found.append(here)
            found.extend(_find_removed_keys(value, here))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_find_removed_keys(item, f"{path}[{i}]"))
    return found


def _require_nonneg_nanoerg(block: Dict[str, Any], key: str, *, strictly_positive: bool) -> None:
    if key not in block:
        raise ConfigValidationError(f"Missing ledgers.ergo.payments.{key}")
    try:
        nano = erg_to_nanoerg(block[key])
    except ValueError as exc:
        raise ConfigValidationError(f"ledgers.ergo.payments.{key}: {exc}") from exc
    if strictly_positive and nano <= 0:
        raise ConfigValidationError(
            f"ledgers.ergo.payments.{key} must be a positive ERG amount, got {block[key]!r}"
        )


def _require_positive_int(block: Dict[str, Any], section: str, key: str) -> None:
    value = block.get(key)
    if value is None:
        raise ConfigValidationError(f"Missing ledgers.ergo.{section}.{key}")
    try:
        as_int = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(
            f"ledgers.ergo.{section}.{key} must be an integer, got {value!r}"
        ) from exc
    if as_int <= 0:
        raise ConfigValidationError(
            f"ledgers.ergo.{section}.{key} must be positive, got {as_int}"
        )


def _require_whole_mu(block: Dict[str, Any], section: str, key: str) -> None:
    """A price is a whole, non-negative number of MU. Absent means 0 (free)."""
    if key not in block:
        return
    raw = block[key]
    try:
        value = Decimal(str(raw if raw not in (None, "") else 0).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigValidationError(f"{section}.{key} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigValidationError(f"{section}.{key} must not be negative, got {value}")
    if value != value.to_integral_value():
        raise ConfigValidationError(
            f"{section}.{key} must be a whole number of MU, got {value}. Prices are in "
            "MU, the node's unit of account; there is nothing smaller to express."
        )


def _require_share(block: Dict[str, Any], section: str, key: str, *, strictly_positive: bool = False) -> None:
    """A share is a fraction in [0, 1] -- or (0, 1] when it divides something."""
    if key not in block:
        return
    try:
        value = float(block[key])
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{section}.{key} must be a number, got {block[key]!r}") from exc
    low_ok = value > 0 if strictly_positive else value >= 0
    if not low_ok or value > 1:
        bound = "(0, 1]" if strictly_positive else "[0, 1]"
        raise ConfigValidationError(f"{section}.{key} must be a share in {bound}, got {value}")


def _require_nonneg_number(block: Dict[str, Any], section: str, key: str) -> None:
    """A quantity that cannot be negative. Absent means 0, which means "no ceiling"."""
    if key not in block:
        return
    try:
        value = float(block[key])
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(
            f"{section}.{key} must be a number, got {block[key]!r}"
        ) from exc
    if value < 0:
        raise ConfigValidationError(f"{section}.{key} must not be negative, got {value}")


HOST_LIMIT_SHARE_KEYS = ("MAX_CPU_SHARE", "MAX_RAM_SHARE", "MAX_DISK_SHARE")
HOST_LIMIT_NET_KEYS = ("MAX_NET_GIB_PER_DAY", "MAX_NET_MIB_PER_SECOND")
ON_CLOSE_VALUES = ("refuse", "stop")


def validate_host_policy_config(config: Dict[str, Any]) -> None:
    """Validate `host_limits` and `activity_window`: how much of the host, and when.

    Both are refusal policies, and a malformed one fails in whichever direction the
    reader happens to guess -- a share that reads as 0 silently lifts a ceiling the
    operator set, and a window that does not parse leaves the node open all night. So
    they are checked here, at load, where the node can still say what is wrong instead
    of behaving as if nothing were.
    """
    limits = config.get("host_limits") or {}
    if not isinstance(limits, dict):
        raise ConfigValidationError("Malformed 'host_limits' mapping.")
    for key in HOST_LIMIT_SHARE_KEYS:
        _require_share(limits, "host_limits", key)
    for key in HOST_LIMIT_NET_KEYS:
        _require_nonneg_number(limits, "host_limits", key)

    window = config.get("activity_window") or {}
    if not isinstance(window, dict):
        raise ConfigValidationError("Malformed 'activity_window' mapping.")

    # Imported here rather than at module scope: this module is loaded from inside
    # ConfigManager.load_config, and activity_window builds a ConfigManager of its own.
    from src.utils.activity_window import parse_clock

    for key in ("START", "END"):
        if key not in window:
            continue
        raw = window[key]
        if parse_clock(str(raw).strip() if raw is not None else "") is None:
            raise ConfigValidationError(
                f"activity_window.{key} must be a time of day as HH:MM, got {raw!r}. "
                "Midnight is 00:00; a window that ends before it starts wraps around it."
            )

    if "ON_CLOSE" in window:
        on_close = str(window["ON_CLOSE"] or "").strip().lower()
        if on_close not in ON_CLOSE_VALUES:
            raise ConfigValidationError(
                f"activity_window.ON_CLOSE must be one of {ON_CLOSE_VALUES}, "
                f"got {window['ON_CLOSE']!r}"
            )


PRICE_KEYS = (
    "RAM_MU_PER_GIB_HOUR",
    "CPU_MU_PER_VCPU_HOUR",
    "DISK_MU_PER_GIB_HOUR",
    "NET_MU_PER_GIB",
    "BUILD_MU",
    "TUNNEL_OPEN_MU",
    "MODIFY_RESOURCES_MU",
)


def validate_pricing_config(config: Dict[str, Any], *, warn=None) -> None:
    """Validate the pricing / free-tier / display / deposit blocks (docs/PRICING.md).

    Prices are money: a malformed one must stop the node rather than be coerced into
    something plausible. A node that silently reads a broken price as 0 gives its
    resources away, and one that reads it as huge refuses every client.

    ``warn`` receives non-fatal findings (a callable taking one string).
    """
    pricing = config.get("pricing") or {}
    if not isinstance(pricing, dict):
        raise ConfigValidationError("Malformed 'pricing' mapping.")
    for key in PRICE_KEYS:
        _require_whole_mu(pricing, "pricing", key)
    _validate_pricing_by_arch(pricing)

    if "SCARCITY_MAX_MULTIPLIER" in pricing:
        try:
            multiplier = int(pricing["SCARCITY_MAX_MULTIPLIER"])
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(
                f"pricing.SCARCITY_MAX_MULTIPLIER must be an integer, got {pricing['SCARCITY_MAX_MULTIPLIER']!r}"
            ) from exc
        if multiplier < 1:
            raise ConfigValidationError(
                f"pricing.SCARCITY_MAX_MULTIPLIER must be at least 1 (1 = no surcharge), got {multiplier}"
            )
    if "SCARCITY_CURVE" in pricing:
        try:
            curve = float(pricing["SCARCITY_CURVE"])
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(
                f"pricing.SCARCITY_CURVE must be a number, got {pricing['SCARCITY_CURVE']!r}"
            ) from exc
        if curve <= 0:
            raise ConfigValidationError(f"pricing.SCARCITY_CURVE must be positive, got {curve}")

    free = config.get("free_tier") or {}
    if not isinstance(free, dict):
        raise ConfigValidationError("Malformed 'free_tier' mapping.")
    _require_whole_mu(free, "free_tier", "CREDIT_MU_PER_NEW_CLIENT")
    _require_share(free, "free_tier", "FREE_WHILE_SCARCITY_BELOW")

    deposits = config.get("deposits") or {}
    if not isinstance(deposits, dict):
        raise ConfigValidationError("Malformed 'deposits' mapping.")
    # Both divide a deposit, so neither may be zero.
    _require_share(deposits, "deposits", "MAX_FEE_OVERHEAD", strictly_positive=True)
    _require_share(deposits, "deposits", "REFILL_BELOW", strictly_positive=True)
    if "INITIAL_RUNTIME_HOURS" in deposits:
        try:
            hours = float(deposits["INITIAL_RUNTIME_HOURS"])
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(
                f"deposits.INITIAL_RUNTIME_HOURS must be a number, got {deposits['INITIAL_RUNTIME_HOURS']!r}"
            ) from exc
        if hours < 0:
            raise ConfigValidationError(
                f"deposits.INITIAL_RUNTIME_HOURS must not be negative, got {hours}"
            )

    rate = _validate_payment_rate(config)
    _validate_display_unit(config, rate)
    _warn_if_charges_cannot_settle(pricing, rate, warn)


def _validate_payment_rate(config: Dict[str, Any]) -> Decimal:
    """``MU_PER_NANOERG``: what the node's unit of account is worth on this ledger."""
    payments = (((config.get("ledgers") or {}).get("ergo") or {}).get("payments") or {})
    raw = payments.get("MU_PER_NANOERG", 1)
    try:
        rate = Decimal(str(raw if raw not in (None, "") else 1).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"ledgers.ergo.payments.MU_PER_NANOERG must be a number, got {raw!r}"
        ) from exc
    if rate <= 0:
        raise ConfigValidationError(
            f"ledgers.ergo.payments.MU_PER_NANOERG must be positive, got {rate}"
        )
    per_erg = rate * 1_000_000_000
    if per_erg != per_erg.to_integral_value():
        raise ConfigValidationError(
            f"ledgers.ergo.payments.MU_PER_NANOERG={rate} makes one ERG {per_erg} MU, "
            "which is not a whole number of MU."
        )
    return rate


def _validate_display_unit(config: Dict[str, Any], rate: Decimal) -> None:
    """``ui.DISPLAY_UNIT`` is presentational, but a broken one breaks every command."""
    ui = config.get("ui") or {}
    if not isinstance(ui, dict):
        raise ConfigValidationError("Malformed 'ui' mapping.")
    name = str(ui.get("DISPLAY_UNIT", "erg") or "erg").strip().lower()
    if name in ("erg", "mu"):
        return

    declared = (ui.get("UNITS") or {}).get(name)
    if not isinstance(declared, dict) or not declared:
        raise ConfigValidationError(
            f"ui.DISPLAY_UNIT is {name!r}, which is neither built in ('erg', 'mu') nor "
            f"declared under ui.UNITS.{name}."
        )
    try:
        unit_rate = Decimal(str(declared.get("MU_PER_UNIT", 0)).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"ui.UNITS.{name}.MU_PER_UNIT must be a number, got {declared.get('MU_PER_UNIT')!r}"
        ) from exc
    if unit_rate <= 0:
        raise ConfigValidationError(f"ui.UNITS.{name}.MU_PER_UNIT must be positive, got {unit_rate}")
    if "DECIMALS" in declared:
        try:
            decimals = int(declared["DECIMALS"])
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(
                f"ui.UNITS.{name}.DECIMALS must be an integer, got {declared['DECIMALS']!r}"
            ) from exc
        if decimals < 0:
            raise ConfigValidationError(f"ui.UNITS.{name}.DECIMALS must not be negative, got {decimals}")


# Prices that may be set per architecture. Only memory: it is the one resource whose
# real cost to the node depends on the guest's arch (the guest kernel reserve the node
# absorbs, which differs per arch). Adding a key here is all it takes to extend the
# set -- `monetary._prices_by_arch` reads any key by name.
PER_ARCH_PRICE_KEYS = (
    "RAM_MU_PER_GIB_HOUR",
)



def _validate_pricing_by_arch(pricing: Dict[str, Any]) -> None:
    """Validate ``pricing.BY_ARCH``: per-architecture price overrides.

    Absent is valid and means every arch pays the scalar prices, so a config that never
    mentions the block needs no change.

    A malformed entry raises, exactly as a malformed scalar price does. A price nobody
    can read is a configuration error, and the two ways of "handling" one are giving
    the node's memory away (read as 0) or refusing every client (read as huge).
    An unrecognised arch tag raises too: it is silently dead config otherwise, and the
    operator who wrote `amd64` instead of `linux/amd64` believes they have set a price
    that never applies to anything. The tags come from
    :data:`src.utils.arch_guard.CANONICAL_ARCHITECTURES`, which is the node's whole
    vocabulary of architectures rather than what this host happens to be able to boot
    -- see there for why validation must not depend on the latter.
    """
    block = pricing.get("BY_ARCH")
    if block is None:
        return
    if not isinstance(block, dict):
        raise ConfigValidationError(
            "Malformed 'pricing.BY_ARCH' mapping: expected one block per architecture, "
            f"got {type(block).__name__}."
        )

    for arch, entry in block.items():
        if arch not in CANONICAL_ARCHITECTURES:
            raise ConfigValidationError(
                f"pricing.BY_ARCH.{arch} is not an architecture this node knows. Use a "
                f"canonical tag: {', '.join(CANONICAL_ARCHITECTURES)}."
            )
        if not isinstance(entry, dict):
            raise ConfigValidationError(
                f"Malformed 'pricing.BY_ARCH.{arch}' mapping: expected price keys, got "
                f"{type(entry).__name__}."
            )
        for key in entry:
            if key not in PER_ARCH_PRICE_KEYS:
                raise ConfigValidationError(
                    f"pricing.BY_ARCH.{arch}.{key} cannot be set per architecture. Only "
                    f"{', '.join(PER_ARCH_PRICE_KEYS)} can: the node hands a guest the "
                    "vCPUs and the image it asked for whatever architecture it is, so "
                    "only memory has a per-arch cost to recover."
                )
            _require_whole_mu(entry, f"pricing.BY_ARCH.{arch}", key)


def _warn_if_charges_cannot_settle(
    pricing: Dict[str, Any], rate: Decimal, warn, *,
    rate_key: str = "ledgers.ergo.payments.MU_PER_NANOERG",
    unit: str = "nanoERG",
) -> None:
    """Do prices and **this** payment system's rate still live on the same scale?

    This is the failure the gas model actually shipped with: charges of order 1e2 and a
    conversion factor of 1e58, so every real charge became zero on-chain and nothing
    could ever be settled. Configuring prices (MU) and the rate (MU per base unit)
    separately makes it reachable again, so it is checked rather than assumed.

    Asked once per registered payment system rather than only of Ergo. A second system
    makes this *more* likely, not less: a satoshi is worth about a million nanoERG, so
    a rate borrowed from one chain by analogy with the other misprices the node by six
    orders of magnitude -- and the direction that matters is per system, because a node
    can be priced correctly for one and absurdly for the other at the same time.

    A warning, not an error: a node may legitimately price everything at zero, and an
    operator mid-edit should not be locked out of their own config.
    """
    if warn is None:
        return
    reference = pricing.get("RAM_MU_PER_GIB_HOUR", 0)
    try:
        reference_mu = Decimal(str(reference if reference not in (None, "") else 0))
    except (InvalidOperation, ValueError, TypeError):
        return
    if reference_mu <= 0 or rate <= 0:
        return
    if reference_mu / rate < 1:
        warn(
            f"pricing.RAM_MU_PER_GIB_HOUR={reference_mu} MU is worth less than one "
            f"{unit} at {rate_key}={rate}, so an hour of a GiB of memory settles as "
            "nothing on-chain. Raise the prices or lower the rate; see docs/PRICING.md."
        )


def _validate_ergo_assets(payments: Dict[str, Any], *, config: Dict[str, Any], warn):
    """``ledgers.ergo.payments.ASSETS``: the tokens this node accepts, if any.

    The rules themselves live in the contract's own rate module and are *called* from
    here rather than restated: a rule enforced at startup but not when the list is read
    gives a config the node boots on and then refuses to settle through, and one
    enforced only when reading surfaces as a payment failure instead of a startup error.

    Refused rather than warned about, unlike the scale checks below: a mistyped token id
    or a duplicated display unit is not a judgement call, and a node that started with
    one would advertise a payment method it cannot honour.
    """
    # Imported here, not at module scope: this module is reached from `utils.config`
    # while it loads, and the rate module reads config through it.
    from src.payment_system.contracts.ergo import rate as ergo_rate

    raw = payments.get("ASSETS") or []
    try:
        assets = ergo_rate.parse_assets(raw)
    except ValueError as exc:
        raise ConfigValidationError(str(exc)) from exc

    declared_units = (config.get("ui") or {}).get("UNITS") or {}
    for asset, entry in zip(assets, raw):
        where = f"ledgers.ergo.payments.ASSETS[{asset.symbol}]"
        if asset.unit_name in declared_units:
            # `monetary.display_unit` prefers what a payment contract contributes, so
            # the operator's own block would be silently ignored rather than clash.
            raise ConfigValidationError(
                f"{where}.UNIT_NAME={asset.unit_name!r} is also declared under "
                f"ui.UNITS.{asset.unit_name}. A unit name has to say which money it is, "
                "so rename one -- the asset's would win, and the hand-declared block "
                "would be read by nobody."
            )
        # Amounts in whole units of the asset, the same decimal-string shape as ERG's.
        # A limit finer than the asset's own decimals cannot be expressed in it at all,
        # and reading as zero would silently sweep or donate everything.
        for key in ("HOT_WALLET_LIMITS", "COLD_WALLET_MIN_TRANSFER",
                    "DONATION_MIN_TRANSFER"):
            if entry.get(key) in (None, ""):
                continue
            try:
                ergo_rate.whole_to_base_units(entry.get(key), asset,
                                              what=f"{where}.{key}")
            except ValueError as exc:
                raise ConfigValidationError(str(exc)) from exc
        # Per method, for the same reason the ledger has its own: what is right for a
        # chain's native unit is absurd for a token priced far from it.
        _require_share(entry, where, "MAX_FEE_OVERHEAD", strictly_positive=True)
        _require_share(entry, where, "DONATION_PERCENTAGE")

    if assets and warn is not None:
        _warn_if_asset_rates_are_implausible(payments, assets, warn)
    return assets


def _warn_if_asset_rates_are_implausible(payments: Dict[str, Any], assets, warn) -> None:
    """Are an asset's rate and ERG's on the same scale?

    ``MU_PER_NANOERG`` and an asset's ``MU_PER_UNIT`` are each "MU per base unit", so
    their ratio *is* this node's opinion about what the token is worth in ERG -- no
    price feed needed. And the two have to be coherent whether or not the operator
    prices anything in ERG, because a token method's fee floor is denominated in ERG
    while its minimum output is in the token: deposit sizing is wrong by exactly the
    ratio between them (#342 4.4).

    A warning rather than an error, like every other scale check here: an operator may
    be mid-edit, and a token really can be worth very little.
    """
    raw = payments.get("MU_PER_NANOERG", 1)
    try:
        mu_per_nanoerg = Decimal(str(raw if raw not in (None, "") else 1).strip())
    except (InvalidOperation, ValueError, TypeError):
        return
    if mu_per_nanoerg <= 0:
        return

    for asset in assets:
        where = f"ledgers.ergo.payments.ASSETS[{asset.symbol}]"
        # value(whole token)/value(ERG), from the two rates alone.
        implied = (asset.mu_per_base_unit * (Decimal(10) ** asset.decimals)) / (
            mu_per_nanoerg * Decimal(1_000_000_000)
        )
        if implied <= 0:
            continue
        if implied < Decimal("0.000000001"):
            warn(
                f"{where}.MU_PER_UNIT={asset.mu_per_base_unit} and "
                f"ledgers.ergo.payments.MU_PER_NANOERG={mu_per_nanoerg} together say "
                f"one whole {asset.symbol} is worth {implied} ERG -- less than a single "
                "nanoERG, so nothing priced in it can settle. One of the two rates is "
                "probably out by a power of ten; see docs/PRICING.md."
            )
        elif implied > Decimal(1_000_000):
            warn(
                f"{where}.MU_PER_UNIT={asset.mu_per_base_unit} and "
                f"ledgers.ergo.payments.MU_PER_NANOERG={mu_per_nanoerg} together say "
                f"one whole {asset.symbol} is worth {implied} ERG, which is not a market "
                "anyone trades in. Check DECIMALS and MU_PER_UNIT: MU_PER_UNIT is MU "
                "per BASE unit, not per whole unit."
            )


def _validate_wallet_list(
    payments: Dict[str, Any], key: str, *, ledger: str, network: str, address_check=None
) -> List[Dict[str, Any]]:
    """One donation wallet list: addresses valid for the chain, weights non-negative.

    Returns the entries, so the caller can tell an empty list from a populated one.
    """
    entries = payments.get(key)
    if entries in (None, ""):
        return []
    where = f"ledgers.{ledger}.payments.{key}"
    if not isinstance(entries, list):
        raise ConfigValidationError(
            f"{where} must be a list of {{address, weight}} entries, got {entries!r}"
        )

    seen: Dict[str, int] = {}
    total = Decimal(0)
    for index, entry in enumerate(entries):
        at = f"{where}[{index}]"
        if not isinstance(entry, dict):
            raise ConfigValidationError(f"{at} must be an {{address, weight}} mapping, got {entry!r}")
        address = str(entry.get("address") or "").strip()
        if not address:
            raise ConfigValidationError(f"{at} has no address.")
        valid = address_check or is_valid_ergo_address
        if not valid(address, network=network):
            raise ConfigValidationError(
                f"{at}.address is not a valid {ledger.capitalize()} address: {address!r}"
            )
        if address in seen:
            # Two rows for one address would double its weight without looking like it.
            raise ConfigValidationError(
                f"{at}.address duplicates {where}[{seen[address]}]: {address!r}. "
                "Give the address one entry with the weight you mean."
            )
        seen[address] = index
        try:
            weight = Decimal(str(entry.get("weight", 0)).strip())
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ConfigValidationError(
                f"{at}.weight must be a number, got {entry.get('weight')!r}"
            ) from exc
        if not weight.is_finite() or weight < 0:
            raise ConfigValidationError(f"{at}.weight must not be negative, got {weight}")
        total += weight

    if entries and total <= 0:
        # Not the same thing as an empty list, and worth saying so: weights are
        # normalised, so a total of zero has nothing to normalise by and the list pays
        # (or counts) nobody -- while looking configured.
        raise ConfigValidationError(
            f"{where} has entries whose weights are all zero, so it splits nothing. "
            "Remove the list to mean 'none', or give at least one entry a weight."
        )
    return entries


def validate_donation_config(
    payments: Dict[str, Any], *, ledger: str = "ergo", network: str = "mainnet", warn=None,
    address_check=None,
) -> None:
    """Validate one ledger's donation block: the share, the two lists, the floors.

    ``address_check`` is the chain's own address validator, because an address only
    means anything on its own chain -- a Bitcoin donation wallet checked against Ergo's
    base58 rules would be refused for the wrong reason, or worse, accepted.
    """
    _require_share(payments, f"ledgers.{ledger}.payments", "DONATION_PERCENTAGE")
    if "DONATION_MIN_TRANSFER" in payments and address_check is None:
        # Ergo's amounts are ERG decimal strings; another ledger's are checked by its
        # own caller, in its own units.
        _require_nonneg_nanoerg(payments, "DONATION_MIN_TRANSFER", strictly_positive=False)
    if "DONATION_MIN_CONFIRMATIONS" in payments:
        raw = payments.get("DONATION_MIN_CONFIRMATIONS")
        try:
            confirmations = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(
                f"ledgers.{ledger}.payments.DONATION_MIN_CONFIRMATIONS must be an "
                f"integer, got {raw!r}"
            ) from exc
        if confirmations < 0:
            raise ConfigValidationError(
                f"ledgers.{ledger}.payments.DONATION_MIN_CONFIRMATIONS must not be "
                f"negative, got {confirmations}"
            )

    pay = _validate_wallet_list(payments, "DONATION_WALLETS", ledger=ledger,
                                network=network, address_check=address_check)
    _validate_wallet_list(payments, "DONATION_CREDIT_WALLETS", ledger=ledger,
                          network=network, address_check=address_check)

    if warn is None:
        return
    try:
        share = Decimal(str(payments.get("DONATION_PERCENTAGE", 0) or 0).strip())
    except (InvalidOperation, ValueError, TypeError):
        return
    if share > 0 and not pay:
        # A warning rather than an error: the node runs, it just donates nothing. It is
        # worth being loud about because the config reads as though it does.
        warn(
            f"ledgers.{ledger}.payments.DONATION_PERCENTAGE is {share} but "
            "DONATION_WALLETS is empty, so nothing is accrued and nothing will ever be "
            "donated. Add a wallet, or set the percentage to 0 to say so on purpose."
        )


# Every parameter of the peer-selection formula, and what it may be.
#
# The weights must not be negative, and that is a design constraint rather than a
# sanity check: a negative donation weight would turn the count list into a punishment
# mechanism, and punishing peers for not donating is exactly what would make patching
# donations out of a node rational.
BALANCER_NONNEG = ("SOCIALIZATION_FACTOR", "DONATION_WEIGHT", "LOCAL_BIAS", "COST_AVERAGE_VARIATION")
BALANCER_POSITIVE = ("REPUTATION_HALF_CREDIT", "DONATION_HALF_CREDIT", "DONATION_AGE_SCALE")


def validate_balancers_config(config: Dict[str, Any]) -> None:
    """Validate the ``balancers:`` block: the shape of the peer-selection formula.

    Absent keys are fine -- each has a default in code, and a node that never edits
    this section gets today's behaviour. What is refused is a value that would make the
    formula meaningless: a negative weight, or a half-credit of zero (which divides).
    """
    balancers = config.get("balancers")
    if balancers in (None, ""):
        return
    if not isinstance(balancers, dict):
        raise ConfigValidationError(f"'balancers' must be a mapping, got {balancers!r}")

    for key in BALANCER_NONNEG:
        _require_nonneg_number(balancers, "balancers", key)
    for key in BALANCER_POSITIVE:
        if key not in balancers:
            continue
        raw = balancers[key]
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ConfigValidationError(
                f"balancers.{key} must be a number, got {raw!r}"
            ) from exc
        if not value.is_finite() or value <= 0:
            raise ConfigValidationError(
                f"balancers.{key} must be positive, got {raw!r}: it is the point at "
                "which half the weight is earned, and the formula divides by it."
            )


BITCOIN_NETWORKS = ("mainnet", "testnet", "signet", "regtest")


def validate_bitcoin_config(config: Dict[str, Any], *, warn=None) -> None:
    """Validate the Bitcoin ledger block, if there is one.

    Structural only: addresses are checked against the configured network with bech32
    and base58check arithmetic, never by asking a node. That matters more here than it
    did for Ergo -- a cold wallet is where an operator's savings go, and validating it
    over RPC would mean a node that cannot reach `bitcoind` accepts a typo silently and
    sweeps to nowhere.
    """
    from src.utils.bitcoin_units import btc_to_satoshi, is_valid_bitcoin_address

    ledgers = config.get("ledgers")
    if not isinstance(ledgers, dict):
        return
    bitcoin = ledgers.get("bitcoin")
    if not isinstance(bitcoin, dict):
        return

    network = str(bitcoin.get("NETWORK") or "mainnet").strip()
    if network not in BITCOIN_NETWORKS:
        raise ConfigValidationError(
            f"ledgers.bitcoin.NETWORK must be one of {', '.join(BITCOIN_NETWORKS)}, "
            f"got {network!r}"
        )

    chosen = str(bitcoin.get("BACKEND") or "core").strip().lower()
    if chosen not in ("core", "esplora"):
        raise ConfigValidationError(
            f"ledgers.bitcoin.BACKEND must be 'core' or 'esplora', got {chosen!r}. "
            "'esplora' is a read-only HTTP API -- the node can be paid in BTC but not "
            "pay in it; 'core' is a bitcoind that holds the wallet and signs."
        )

    payments = bitcoin.get("payments")
    if not isinstance(payments, dict):
        return

    for key in ("HOT_WALLET_LIMITS", "COLD_WALLET_MIN_TRANSFER", "DONATION_MIN_TRANSFER"):
        if payments.get(key) in (None, ""):
            continue
        try:
            btc_to_satoshi(payments[key])
        except ValueError as exc:
            raise ConfigValidationError(f"ledgers.bitcoin.payments.{key}: {exc}") from exc

    for key in ("COLD_WALLET", "RECEIVING_ADDRESS"):
        address = str(payments.get(key) or "").strip()
        if address and not is_valid_bitcoin_address(address, network=network):
            raise ConfigValidationError(
                f"ledgers.bitcoin.payments.{key} is not a valid {network} Bitcoin "
                f"address: {address!r}. An address valid on another network is refused "
                "too -- sweeping to it would send funds nobody on this chain can spend."
            )

    rate = payments.get("MU_PER_SATOSHI")
    # An unset rate means this node does not offer Bitcoin at all -- the shipped default
    # -- so everything below is still checked for shape but nothing is *warned* about.
    # A dormant ledger must not talk at every startup about a donation it will never pay.
    offered = rate not in (None, "")
    dormant_warn = warn if offered else None

    _require_share(payments, "ledgers.bitcoin.payments", "DONATION_PERCENTAGE")
    _require_share(payments, "ledgers.bitcoin.payments", "MAX_FEE_OVERHEAD",
                   strictly_positive=True)
    validate_donation_config(payments, ledger="bitcoin", network=network,
                             warn=dormant_warn, address_check=is_valid_bitcoin_address)

    if not offered:
        return
    try:
        rate_value = Decimal(str(rate).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"ledgers.bitcoin.payments.MU_PER_SATOSHI must be a number, got {rate!r}"
        ) from exc
    if rate_value <= 0:
        raise ConfigValidationError(
            f"ledgers.bitcoin.payments.MU_PER_SATOSHI must be positive, got {rate_value}"
        )
    if warn is not None and rate_value == 1:
        # The specific mistake worth naming: 1 is what MU_PER_NANOERG is, and copying it
        # here misprices the node by about a million.
        warn(
            "ledgers.bitcoin.payments.MU_PER_SATOSHI is 1, which is MU_PER_NANOERG's "
            "value. A satoshi is worth about a million nanoERG, so this sells an hour "
            "of compute for roughly a millionth of its price. See docs/BITCOIN.md."
        )

    # And the check that two payment systems are on *compatible* scales, which needs no
    # market data at all: their two rates imply an exchange rate between the chains, and
    # an implausible one is a rate borrowed from the other by analogy.
    #
    # Deliberately not Ergo's per-charge settle-check applied here. On-chain Bitcoin
    # cannot settle a single GiB-hour by design -- §5 of the issue that added this
    # spells out the arithmetic, and the prepaid-deposit model is what absorbs it -- so
    # that check fires on a correctly configured node, and a warning that sounds on the
    # shipped config trains operators to ignore warnings.
    if warn is not None:
        _warn_if_the_two_rates_disagree(config, rate_value, warn)


def _warn_if_the_two_rates_disagree(config: Dict[str, Any], mu_per_satoshi: Decimal,
                                    warn) -> None:
    """Do this node's two payment rates imply a believable world?

    ``MU_PER_NANOERG`` and ``MU_PER_SATOSHI`` are each "MU per base unit", so their
    ratio *is* this node's opinion about what a satoshi is worth in nanoERG -- and since
    one BTC is 1e8 satoshi and one ERG is 1e9 nanoERG, they imply a BTC/ERG price
    without anybody having to supply one.

    A config implying that one BTC is worth less than one ERG is not a market view, it
    is a rate copied from the other chain by analogy. That is the mistake worth catching
    here, and it is catchable with arithmetic rather than with a price feed.
    """
    ergo = ((config.get("ledgers") or {}).get("ergo") or {}).get("payments") or {}
    raw = ergo.get("MU_PER_NANOERG")
    if raw in (None, ""):
        return
    try:
        mu_per_nanoerg = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        return
    if mu_per_nanoerg <= 0 or mu_per_satoshi <= 0:
        return

    # value(BTC)/value(ERG) = (1e8 x value(sat)) / (1e9 x value(nanoERG))
    implied = (mu_per_satoshi / mu_per_nanoerg) / 10
    if implied < 1:
        warn(
            f"ledgers.bitcoin.payments.MU_PER_SATOSHI={mu_per_satoshi} and "
            f"ledgers.ergo.payments.MU_PER_NANOERG={mu_per_nanoerg} together say one "
            f"BTC is worth {implied} ERG, which is not a market anyone trades in. One "
            "of the two rates was probably copied from the other; see docs/BITCOIN.md."
        )


def validate_ergo_config(
    config: Dict[str, Any],
    *,
    payments_enabled: bool = True,
    reputation_enabled: bool = True,
    network: str = "mainnet",
    warn=None,
) -> None:
    """
    Validate the Ergo section of a fully-loaded config mapping. Raises
    :class:`ConfigValidationError` on the first problem; returns ``None`` when valid.
    """
    removed = _find_removed_keys(config)
    if removed:
        raise ConfigValidationError(
            "Removed configuration keys are still present (no migration is provided; "
            f"update the config manually): {', '.join(sorted(removed))}"
        )

    # Unconditional, unlike every ledger check below: a node without an identity has no
    # peer_id, so it can neither serve nor dial -- with payments and reputation switched
    # off and every ledger removed, it still needs a name.
    identity = config.get("identity")
    if not isinstance(identity, dict) or not (identity.get("MNEMONIC") or ""):
        raise ConfigValidationError(
            "identity.MNEMONIC is required: it is the key this node is named by. "
            "Leave it empty in the file and one is generated on first load."
        )

    ledgers = config.get("ledgers")
    if not isinstance(ledgers, dict):
        raise ConfigValidationError("Missing or malformed 'ledgers' mapping.")
    ergo = ledgers.get("ergo")
    if not isinstance(ergo, dict):
        # No Ergo ledger configured; nothing more to validate.
        return

    mnemonic = ergo.get("WALLET_MNEMONIC") or ""
    if (payments_enabled or reputation_enabled) and not mnemonic:
        raise ConfigValidationError(
            "ledgers.ergo.WALLET_MNEMONIC is required when payments or reputation are enabled."
        )

    if payments_enabled:
        payments = ergo.get("payments")
        if not isinstance(payments, dict):
            raise ConfigValidationError("Missing ledgers.ergo.payments block.")
        # HOT_WALLET_LIMITS may be 0 (sweep everything); COLD_WALLET_MIN_TRANSFER must be > 0.
        _require_nonneg_nanoerg(payments, "HOT_WALLET_LIMITS", strictly_positive=False)
        _require_nonneg_nanoerg(payments, "COLD_WALLET_MIN_TRANSFER", strictly_positive=True)
        cold = payments.get("COLD_WALLET") or ""
        if cold and not is_valid_ergo_address(cold, network=network):
            raise ConfigValidationError(
                f"ledgers.ergo.payments.COLD_WALLET is not a valid Ergo address: {cold!r}"
            )
        _validate_ergo_assets(payments, config=config, warn=warn)
        validate_donation_config(payments, ledger="ergo", network=network, warn=warn)

    if reputation_enabled:
        reputation = ergo.get("reputation")
        if not isinstance(reputation, dict):
            raise ConfigValidationError("Missing ledgers.ergo.reputation block.")
        _require_positive_int(reputation, "reputation", "LEDGER_REPUTATION_SUBMISSION_THRESHOLD")
        _require_positive_int(reputation, "reputation", "TOTAL_REPUTATION_TOKEN_AMOUNT")
