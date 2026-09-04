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


def _warn_if_charges_cannot_settle(pricing: Dict[str, Any], rate: Decimal, warn) -> None:
    """Do prices and the payment rate still live on the same scale?

    This is the failure the gas model actually shipped with: charges of order 1e2 and a
    conversion factor of 1e58, so every real charge became zero on-chain and nothing
    could ever be settled. Configuring prices (MU) and the rate (MU per nanoERG)
    separately makes it reachable again, so it is checked rather than assumed.

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
    if reference_mu <= 0:
        return
    if reference_mu / rate < 1:
        warn(
            f"pricing.RAM_MU_PER_GIB_HOUR={reference_mu} MU is worth less than one "
            f"nanoERG at ledgers.ergo.payments.MU_PER_NANOERG={rate}, so an hour of a "
            "GiB of memory settles as nothing on-chain. Raise the prices or lower the "
            "rate; see docs/PRICING.md."
        )


def validate_ergo_config(
    config: Dict[str, Any],
    *,
    payments_enabled: bool = True,
    reputation_enabled: bool = True,
    network: str = "mainnet",
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

    if reputation_enabled:
        reputation = ergo.get("reputation")
        if not isinstance(reputation, dict):
            raise ConfigValidationError("Missing ledgers.ergo.reputation block.")
        _require_positive_int(reputation, "reputation", "LEDGER_REPUTATION_SUBMISSION_THRESHOLD")
        _require_positive_int(reputation, "reputation", "TOTAL_REPUTATION_TOKEN_AMOUNT")
