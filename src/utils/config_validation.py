"""
Validation for the single-wallet Ergo configuration.

This is a *breaking* pre-production layout: the old ``reputation`` / ``payments`` root
blocks, the auxiliary/receiver wallet, and the ``PAYMENTS_RECEIVER_WALLET`` key (and its
historical ``PAYMENTS_RECIVER_WALLET`` typo) are gone. A config still carrying any of
those keys is rejected outright — there is no migration or fallback.
"""
from __future__ import annotations

from typing import Any, Dict, List

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


def _require_erg_amount(block: Dict[str, Any], section: str, key: str) -> None:
    """A price must be a parseable, non-negative ERG amount. Absent means 0 (free)."""
    if key not in block:
        return
    try:
        erg_to_nanoerg(block[key] if block[key] not in (None, "") else "0")
    except ValueError as exc:
        raise ConfigValidationError(f"{section}.{key}: {exc}") from exc


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


def validate_pricing_config(config: Dict[str, Any]) -> None:
    """Validate the pricing / free-tier / deposit blocks (docs/PRICING.md).

    Prices are money: a malformed one must stop the node rather than be coerced to
    something plausible. A node that silently reads a broken price as 0 gives its
    resources away, and one that reads it as huge refuses every client.
    """
    pricing = config.get("pricing") or {}
    if not isinstance(pricing, dict):
        raise ConfigValidationError("Malformed 'pricing' mapping.")
    for key in (
        "RAM_ERG_PER_GIB_HOUR",
        "CPU_ERG_PER_VCPU_HOUR",
        "DISK_ERG_PER_GIB_HOUR",
        "NET_ERG_PER_GIB",
        "BUILD_ERG",
        "TUNNEL_OPEN_ERG",
        "MODIFY_RESOURCES_ERG",
    ):
        _require_erg_amount(pricing, "pricing", key)

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
    _require_erg_amount(free, "free_tier", "CREDIT_ERG_PER_NEW_CLIENT")
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
