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
    DEFAULT_EXTERNAL_TIMEOUT_SECONDS,
    EnergyBackend,
    HwmonBackend,
    IpmiBackend,
    ModelBackend,
    NvmlBackend,
    RaplBackend,
    SmartPlugBackend,
    first_reading,
    package_power_limit_watts,
    with_additions,
)
from src.manager.energy.cgroup import (
    CpuWeightTracker,
    instance_cgroup_dir,
    read_usage_usec,
)
from src.manager.energy.price import FixedPriceSource, PriceSource, Tariff
from src.utils.config import ConfigManager
from src.utils import logger as log

LOG_PREFIX = "[energy]"

DEFAULT_ENABLED = True
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_PRICE_PER_KWH = 0.0
DEFAULT_CURRENCY = "EUR"
# The model's coefficients have no defaults worth shipping: a number that fits one
# machine is an order of magnitude wrong on another. 0 means uncalibrated, and an
# uncalibrated model reports nothing at all.
DEFAULT_IDLE_WATTS = 0.0
DEFAULT_LOAD_WATTS = 0.0
MIN_INTERVAL_SECONDS = 5

_last_tick_monotonic: Optional[float] = None
_cpu_weights = CpuWeightTracker()
_rapl = RaplBackend()
# Price sources live as long as the process, so one that caches a day-ahead curve
# keeps it between ticks instead of refetching every interval. Safe to hold: config
# is read once per process, so a source built from it cannot go stale.
_price_sources: Dict[str, PriceSource] = {}
_unknown_price_sources: Set[str] = set()


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


def _fixed_price_source() -> PriceSource:
    """The configured flat tariff. The floor every other source falls back to."""
    return FixedPriceSource(
        price_per_kwh=_as_float(
            _setting("PRICE_PER_KWH", DEFAULT_PRICE_PER_KWH),
            DEFAULT_PRICE_PER_KWH,
        ),
        currency=str(_setting("CURRENCY", DEFAULT_CURRENCY) or DEFAULT_CURRENCY),
    )


# Where a new source is registered: a name, and something that builds it once.
_PRICE_SOURCE_BUILDERS: Dict[str, Callable[[], PriceSource]] = {
    "fixed": _fixed_price_source,
}


def _price_source() -> PriceSource:
    """The configured source, built on first use and kept for the process.

    A name with no builder falls back to the fixed tariff, and says so once rather
    than once per sample: a typo in config must not write a log line every
    interval for the life of the node.
    """
    name = str(_setting("PRICE_SOURCE", "fixed") or "fixed").strip().lower()
    build = _PRICE_SOURCE_BUILDERS.get(name)
    if build is None:
        if name not in _unknown_price_sources:
            _unknown_price_sources.add(name)
            log.LOGGER(
                f"{LOG_PREFIX} energy.PRICE_SOURCE={name!r} is not implemented; "
                "using the fixed tariff."
            )
        name, build = "fixed", _fixed_price_source
    source = _price_sources.get(name)
    if source is None:
        source = build()
        _price_sources[name] = source
    return source


def _tariff() -> Tariff:
    return _price_source().current()


def _cgroups_base() -> Path:
    raw = _config().get("virtualizers.ch.CGROUPS_BASE_DIR", "/sys/fs/cgroup")
    return Path(str(raw) if raw else "/sys/fs/cgroup")


def _cpu_percent() -> float:
    """Host CPU use since the previous call, as a percentage.

    Read exactly once per tick and passed around, never called twice in a tick:
    ``psutil.cpu_percent(interval=None)`` measures against its own last call, so a
    second call moments later reports the near-zero interval between the two rather
    than the sampling interval.
    """
    try:
        import psutil

        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return 0.0


def _idle_watts() -> float:
    """Watts the machine draws doing nothing, as measured by the operator.

    Nothing on the host publishes this, so there is no fallback: 0 leaves the model
    uncalibrated and silent.
    """
    return _as_float(_setting("IDLE_WATTS", DEFAULT_IDLE_WATTS), DEFAULT_IDLE_WATTS)


@lru_cache(maxsize=1)
def _declared_load_watts() -> float:
    """The packages' sustained power limit, memoised. A machine's ceiling is fixed."""
    limit = package_power_limit_watts(_rapl.root)
    return float(limit) if limit else 0.0


def _load_watts() -> float:
    """Extra watts at 100% CPU: the operator's figure, else the declared ceiling.

    An operator who measured the machine at full load has the better number, since
    it includes RAM, disks and the power supply's losses. Where nobody measured,
    the packages' own long-term limit is at least real and specific to this
    machine, and it is readable when the energy counter is not.
    """
    configured = _as_float(
        _setting("LOAD_WATTS", DEFAULT_LOAD_WATTS), DEFAULT_LOAD_WATTS
    )
    if configured > 0:
        return configured
    return _declared_load_watts()


def _busy_cores(cpu_percent: float) -> float:
    """Host CPU use over the interval expressed in cores, to match cgroup weights.

    A cgroup's ``usage_usec`` delta is core-time, so the host figure it is measured
    against has to be core-time too: 50% of an eight-core machine is four cores.
    """
    cores = os.cpu_count() or 1
    return max(0.0, float(cpu_percent)) / 100.0 * cores


def _external_timeout() -> float:
    return _as_float(
        _setting("EXTERNAL_TIMEOUT_SECONDS", DEFAULT_EXTERNAL_TIMEOUT_SECONDS),
        DEFAULT_EXTERNAL_TIMEOUT_SECONDS,
    )


@lru_cache(maxsize=1)
def _measuring_backends() -> List[EnergyBackend]:
    """The configured sources that measure, most complete first.

    Order is the whole point: a plug reads the socket, a BMC reads the supply's
    input, a rail and a CPU package read a part. `first_reading` takes the first
    that answers, so the best figure the machine can give has to come first.

    A source the operator has not configured is left out rather than asked and
    ignored, which is what keeps a node that configures none of them from paying
    for a subprocess or an HTTP request every tick. Built once: some of them hold
    the previous counter, and config is read once per process anyway.
    """
    timeout = _external_timeout()
    backends: List[EnergyBackend] = []

    plug_url = str(_setting("SMART_PLUG_URL", "") or "").strip()
    if plug_url:
        backends.append(
            SmartPlugBackend(
                url=plug_url,
                power_path=str(_setting("SMART_PLUG_POWER_PATH", "power") or "power"),
                timeout_seconds=timeout,
            )
        )
    if _as_bool(_setting("IPMI_ENABLED", False), False):
        backends.append(IpmiBackend(timeout_seconds=timeout))

    chip = str(_setting("HWMON_CHIP", "") or "").strip()
    sensor = str(_setting("HWMON_SENSOR", "") or "").strip()
    if chip and sensor:
        backends.append(HwmonBackend(chip=chip, sensor=sensor))

    backends.append(_rapl)
    return backends


@lru_cache(maxsize=1)
def _additive_backends() -> List[EnergyBackend]:
    """Sources whose draw adds to a partial reading instead of replacing it.

    See `with_additions`: these are asked only when the measured figure is a
    floor, since a source that already covers the machine has them in it.
    """
    if not _as_bool(_setting("NVML_ENABLED", False), False):
        return []
    return [NvmlBackend(timeout_seconds=_external_timeout())]


def _backends(cpu_percent: float) -> List[EnergyBackend]:
    # The model goes last: it is the only one that answers without measuring, so
    # it must not shadow a source that does.
    return _measuring_backends() + [
        ModelBackend(
            idle_watts=_idle_watts(),
            load_watts=_load_watts(),
            cpu_percent_fn=lambda: cpu_percent,
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

    The first call has no interval behind it. Pretending the sample covers
    SAMPLE_INTERVAL_SECONDS would invent joules for time that has not passed.
    Every measuring source is sampled and the answer thrown away, because the
    ones that read a counter -- RAPL, an hwmon energy sensor -- have nothing to
    subtract from until they have read it once. ``psutil.cpu_percent`` is primed
    for the same reason: its first answer is a documented 0.0 with nothing behind
    it. The cost is one round of whatever the configured sources cost, once.
    """
    for backend in _measuring_backends():
        backend.sample(1.0)
    _cpu_percent()
    usage = _instance_usage_usec()
    _cpu_weights.weights(usage, 0.0)


def _sample(elapsed_seconds: float) -> None:
    cpu_percent = _cpu_percent()
    reading = with_additions(
        first_reading(_backends(cpu_percent), elapsed_seconds),
        _additive_backends(),
        elapsed_seconds,
    )
    if reading is None:
        return
    tariff = _tariff()
    usage = _instance_usage_usec()
    weights = _cpu_weights.weights(usage, elapsed_seconds)
    idle = 0.0
    if not reading.is_floor:
        # A figure that covers the machine includes the draw it has at rest, and
        # no instance caused that. A floor is a package or a rail, which an
        # at-the-wall idle figure does not describe -- subtracting 30 W of house
        # from an 8 W package would leave nothing to attribute at all.
        idle = min(_idle_watts(), reading.watts)
    attributed = attribute(
        node_watts=reading.watts,
        weights=weights,
        busy_cores=_busy_cores(cpu_percent),
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
