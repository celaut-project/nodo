"""Pluggable energy measurement backends.

RAPL is the day-one source: ``/sys/class/powercap/intel-rapl/`` energy counters
in microjoules, no extra hardware, no operator configuration. It is CPU-package
only — GPU, disk and PSU losses are not included — so a RAPL reading is a
*floor*, not wall-socket consumption.

Where RAPL is unreadable (permissions, non-x86, VMs, WSL), a model estimator
kicks in: idle watts plus a linear term in CPU utilisation. Coefficients live
in config so an operator can tune them per machine.

IPMI, NVML and a smart plug belong behind the same ``EnergyBackend`` protocol
later; they are not implemented here.

This module imports nothing from the rest of nodo so the math can be tested
without ``bee_rpc``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Tuple


RAPL_ROOT = Path("/sys/class/powercap/intel-rapl")
MICROJOULES_PER_JOULE = 1_000_000.0


@dataclass(frozen=True)
class EnergyReading:
    """One interval's measured (or estimated) energy.

    ``joules`` is energy *in this interval*, not a cumulative counter.
    ``is_floor`` is True for RAPL (package-only) and False for the model.
    """

    joules: float
    watts: float
    backend: str
    is_floor: bool


class EnergyBackend(Protocol):
    """Something that can produce an :class:`EnergyReading` for an interval.

    ``sample`` returns None when this backend cannot produce a reading yet
    (first RAPL snapshot has no delta) or at all (RAPL sysfs missing). The
    monitor then tries the next backend. Implementations must not raise on
    missing hardware.
    """

    name: str

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        ...


def _read_int(path: Path) -> Optional[int]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def package_domain_dirs(root: Path) -> List[Path]:
    """Package-level RAPL domains under ``root``.

    ``intel-rapl:0``, ``intel-rapl:1`` are packages. ``intel-rapl:0:0`` is a
    subdomain (DRAM, core, …) already counted in the package total; including
    those would double-count.
    """
    if not root.is_dir():
        return []
    packages: List[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for path in sorted(entries, key=lambda p: p.name):
        name = path.name
        if not name.startswith("intel-rapl:"):
            continue
        rest = name.split("intel-rapl:", 1)[1]
        if ":" in rest:
            continue
        if path.is_dir():
            packages.append(path)
    return packages


def read_package_counters(root: Path) -> Optional[Tuple[int, int]]:
    """Sum ``energy_uj`` and ``max_energy_range_uj`` across package domains.

    Returns None when no package is readable. A package missing
    ``max_energy_range_uj`` still contributes its energy; wrap handling then
    has no range for that package and a wrap is treated as unreadable.
    """
    total_uj = 0
    total_range = 0
    saw_any = False
    ranges_complete = True
    for domain in package_domain_dirs(root):
        energy = _read_int(domain / "energy_uj")
        if energy is None:
            continue
        saw_any = True
        total_uj += energy
        rng = _read_int(domain / "max_energy_range_uj")
        if rng is None or rng <= 0:
            ranges_complete = False
        else:
            total_range += rng
    if not saw_any:
        return None
    return total_uj, total_range if ranges_complete else 0


def wrapped_delta(previous: int, current: int, max_range: int) -> Optional[int]:
    """Energy-counter delta, including a single wrap of ``max_range``.

    RAPL counters are unsigned and wrap at ``max_energy_range_uj``. Without
    the range a decreasing reading cannot be interpreted, so we return None
    rather than invent a huge negative-turned-positive spike.
    """
    if current >= previous:
        return current - previous
    if max_range <= 0:
        return None
    return (max_range - previous) + current


class RaplBackend:
    """CPU-package RAPL. Readings are a floor, not wall-socket watts."""

    name = "rapl"

    def __init__(self, root: Path = RAPL_ROOT):
        self.root = Path(root)
        self._previous_uj: Optional[int] = None
        self._max_range_uj: int = 0

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        counters = read_package_counters(self.root)
        if counters is None:
            return None
        current_uj, max_range_uj = counters
        previous = self._previous_uj
        self._previous_uj = current_uj
        self._max_range_uj = max_range_uj
        if previous is None:
            return None
        if elapsed_seconds <= 0:
            return None
        delta_uj = wrapped_delta(previous, current_uj, max_range_uj)
        if delta_uj is None:
            return None
        joules = delta_uj / MICROJOULES_PER_JOULE
        watts = joules / elapsed_seconds
        if watts < 0:
            return None
        return EnergyReading(
            joules=joules,
            watts=watts,
            backend=self.name,
            is_floor=True,
        )


class ModelBackend:
    """Idle + linear-in-utilisation estimator. Always available.

    ``cpu_percent_fn`` must be non-blocking. ``psutil.cpu_percent(interval=None)``
    returns 0.0 on the first call; that is accepted (the sample then reports
    idle watts only) rather than sleeping 100ms on the manager thread.
    """

    name = "model"

    def __init__(
        self,
        idle_watts: float,
        load_watts: float,
        cpu_percent_fn: Callable[[], float],
    ):
        self.idle_watts = max(0.0, float(idle_watts))
        self.load_watts = max(0.0, float(load_watts))
        self._cpu_percent_fn = cpu_percent_fn

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        if elapsed_seconds <= 0:
            return None
        try:
            cpu_percent = float(self._cpu_percent_fn())
        except Exception:
            cpu_percent = 0.0
        if cpu_percent < 0:
            cpu_percent = 0.0
        if cpu_percent > 100:
            cpu_percent = 100.0
        watts = self.idle_watts + self.load_watts * (cpu_percent / 100.0)
        joules = watts * elapsed_seconds
        return EnergyReading(
            joules=joules,
            watts=watts,
            backend=self.name,
            is_floor=False,
        )


def first_reading(
    backends: Iterable[EnergyBackend],
    elapsed_seconds: float,
) -> Optional[EnergyReading]:
    """Try backends in order; the first that returns a reading wins."""
    for backend in backends:
        reading = backend.sample(elapsed_seconds)
        if reading is not None:
            return reading
    return None


def model_watts(idle_watts: float, load_watts: float, cpu_percent: float) -> float:
    """Pure estimator, exposed for tests and for callers that already have CPU%."""
    cpu = min(100.0, max(0.0, float(cpu_percent)))
    return max(0.0, float(idle_watts)) + max(0.0, float(load_watts)) * (cpu / 100.0)
