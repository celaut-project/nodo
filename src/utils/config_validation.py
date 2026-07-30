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

# Keys that were removed in the single-wallet refactor. Their presence anywhere in the
# config means the file predates this change and must be updated by hand.
REMOVED_KEYS = (
    "AUXILIARY_MNEMONIC",
    "AUXILIAR_MNEMONIC",
    "PAYMENTS_RECEIVER_WALLET",
    "PAYMENTS_RECIVER_WALLET",
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
