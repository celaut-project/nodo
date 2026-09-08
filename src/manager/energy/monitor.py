"""Sampling tick: measure, attribute, persist.

Wired into ``manager_thread`` as ``energy_tick()``, same shape as ``ddns_tick``:
calling it more often than ``energy.SAMPLE_INTERVAL_SECONDS`` is a cheap no-op,
and it never raises.

Every setting goes through ``_setting``, which answers with a default when the key
is absent, so a config that does not mention ``energy`` at all still imports and
runs. ``ConfigManager`` reads config.yaml once per process, so a changed tariff or
a flipped ENABLED takes effect when the daemon restarts.
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from src.manager.energy.attribution import attribute
from src.manager.energy.backends import (
    EnergyBackend,
    ModelBackend,
    RaplBackend,
    first_reading,
)
from src.manager.energy.cgroup import (
    CpuWeightTracker,
    instance_cgroup_dir,
    read_usage_usec,
)
from src.manager.energy.price import FixedPriceSource, Tariff
from src.utils.config import ConfigManager
from src.utils import logger as log

LOG_PREFIX = "[energy]"

DEFAULT_ENABLED = True
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_PRICE_PER_KWH = 0.0
DEFAULT_CURRENCY = "EUR"
DEFAULT_IDLE_WATTS = 30.0
DEFAULT_LOAD_WATTS = 120.0
MIN_INTERVAL_SECONDS = 5

_last_tick_monotonic: Optional[float] = None
_cpu_weights = CpuWeightTracker()
_rapl = RaplBackend()


def _config() -> ConfigManager:
    return ConfigManager()


def _setting(key: str, default):
    try:
        value = _config().get(f"energy.{key}", default)
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    return _as_bool(_setting("ENABLED", DEFAULT_ENABLED), DEFAULT_ENABLED)


def interval_seconds() -> int:
    configured = _as_int(
        _setting("SAMPLE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS),
        DEFAULT_INTERVAL_SECONDS,
    )
    if configured < MIN_INTERVAL_SECONDS:
        return MIN_INTERVAL_SECONDS
    return configured


def _tariff() -> Tariff:
    source_name = str(_setting("PRICE_SOURCE", "fixed") or "fixed").strip().lower()
    # Only "fixed" is implemented. Anything else falls back rather than hanging
    # the tick on an HTTP client that does not exist.
    if source_name != "fixed":
        log.LOGGER(
            f"{LOG_PREFIX} energy.PRICE_SOURCE={source_name!r} is not implemented; "
            "using fixed."
        )
    return FixedPriceSource(
        price_per_kwh=_as_float(
            _setting("PRICE_PER_KWH", DEFAULT_PRICE_PER_KWH),
            DEFAULT_PRICE_PER_KWH,
        ),
        currency=str(_setting("CURRENCY", DEFAULT_CURRENCY) or DEFAULT_CURRENCY),
    ).current()


def _cgroups_base() -> Path:
    raw = _config().get("virtualizers.ch.CGROUPS_BASE_DIR", "/sys/fs/cgroup")
    return Path(str(raw) if raw else "/sys/fs/cgroup")


def _cpu_percent() -> float:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return 0.0


def _backends() -> List[EnergyBackend]:
    idle = _as_float(_setting("IDLE_WATTS", DEFAULT_IDLE_WATTS), DEFAULT_IDLE_WATTS)
    load = _as_float(_setting("LOAD_WATTS", DEFAULT_LOAD_WATTS), DEFAULT_LOAD_WATTS)
    return [
        _rapl,
        ModelBackend(
            idle_watts=idle,
            load_watts=load,
            cpu_percent_fn=_cpu_percent,
        ),
    ]


def _instance_usage_usec() -> Dict[str, Optional[int]]:
    """Current ``usage_usec`` for every local instance, or None if unreadable."""
    from src.database.sql_connection import SQLConnection

    base = _cgroups_base()
    usage: Dict[str, Optional[int]] = {}
    try:
        ids = SQLConnection().get_all_internal_containers_ids()
    except Exception as exc:
        log.LOGGER(f"{LOG_PREFIX} could not list local instances: {exc}")
        return usage
    for instance_id in ids:
        try:
            cgroup = instance_cgroup_dir(instance_id, base)
        except ValueError:
            continue
        usage[instance_id] = read_usage_usec(cgroup)
    return usage


def _persist(reading, tariff: Tariff, attributed, elapsed_seconds: float) -> None:
    from src.database.sql_connection import SQLConnection

    sc = SQLConnection()
    sc.insert_energy_sample(
        energy_joules=reading.joules,
        watts=reading.watts,
        price_per_kwh=tariff.price_per_kwh,
        currency=tariff.currency,
        backend=reading.backend,
        is_floor=bool(reading.is_floor),
    )
    sc.replace_instance_energy(
        {
            instance_id: (watts, attributed.instance_share.get(instance_id, 0.0))
            for instance_id, watts in attributed.instance_watts.items()
        }
    )


def _prime() -> None:
    """Take a RAPL/cgroup snapshot so the next tick has a real delta.

    The first call has no interval yet. Pretending the sample covers
    SAMPLE_INTERVAL_SECONDS would invent joules for time that has not
    passed. RAPL's first ``sample`` already returns None (no previous
    counter); we still call it so the counter is stored.
    """
    _rapl.sample(1.0)
    usage = _instance_usage_usec()
    _cpu_weights.weights(usage, 0.0)


def _sample(elapsed_seconds: float) -> None:
    reading = first_reading(_backends(), elapsed_seconds)
    if reading is None:
        return
    tariff = _tariff()
    usage = _instance_usage_usec()
    weights = _cpu_weights.weights(usage, elapsed_seconds)
    idle = 0.0
    if reading.backend == "model":
        idle = _as_float(_setting("IDLE_WATTS", DEFAULT_IDLE_WATTS), DEFAULT_IDLE_WATTS)
        idle = min(idle, reading.watts)
    attributed = attribute(
        node_watts=reading.watts,
        weights=weights,
        idle_watts=idle,
    )
    _persist(reading, tariff, attributed, elapsed_seconds)


def energy_tick() -> None:
    """Manager-loop hook. Self-gates; never raises."""
    global _last_tick_monotonic
    try:
        if not is_enabled():
            return
        now = time.monotonic()
        last = _last_tick_monotonic
        if last is None:
            _prime()
            _last_tick_monotonic = now
            return
        if (now - last) < interval_seconds():
            return
        elapsed = now - last
        _last_tick_monotonic = now
        _sample(elapsed)
    except Exception as exc:
        log.LOGGER(f"{LOG_PREFIX} tick failed: {exc}")
