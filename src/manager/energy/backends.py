"""Pluggable energy measurement backends.

RAPL is the first source tried: ``/sys/class/powercap/intel-rapl/`` energy
counters in microjoules, no extra hardware and no operator configuration. The
path carries Intel's name because ``intel_rapl_msr`` is the driver behind it, and
that same driver serves AMD Zen, so the directory and the domain layout are what
an AMD machine exposes too. It is CPU-package only — GPU, disk and PSU losses are
not included — so a RAPL reading is a *floor*, not wall-socket consumption.

Where there is no readable counter — ``energy_uj`` is root-only on current
kernels, and Apple Silicon under Asahi, ARM boards, VMs and WSL have no RAPL at
all — a model estimator can take over: idle watts plus a linear term in CPU
utilisation. Its two coefficients have to be measured with a meter, so a machine
whose operator has not measured them reports nothing. An invented number reads
exactly like a measured one once it is on screen and in the database.

The backends that need hardware a node cannot be assumed to have are declared
below with what each one would measure, and produce no reading.

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


class HwmonBackend:
    """A power or energy sensor the kernel publishes as hwmon. Unimplemented.

    The reading for machines with no RAPL at all. On Apple Silicon under Asahi the
    ``macsmc`` drivers surface the SMC's rails here, which is the only power figure
    an M-series Mac offers Linux; ARM boards with a shunt (INA219 and kin) and
    server boards exposing PMBus rails land in the same place. A sensor answers in
    microwatts (``powerN_input``, a rate, so an implementation multiplies by
    ``elapsed_seconds``) or in microjoules (``energyN_input``, a counter to
    subtract, like RAPL).

    What a rail actually covers is a property of the board and is not discoverable:
    one machine's ``power1_input`` is the whole SoC, another's is a single 12V line,
    and this host's is the battery. So an implementation cannot pick a sensor by
    scanning -- config has to name the chip and the rail, and the operator has to
    know what they named. That, not the reading, is the work.

    ``power_supply`` is not the way in either, though ``power_now`` looks like the
    whole machine: on a laptop it measures battery discharge, so it reads zero for
    the mains-powered machine a node is expected to be.
    """

    name = "hwmon"

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        return None


class IpmiBackend:
    """Whole-machine draw from the board's management controller. Unimplemented.

    A server's BMC exposes the power the PSU is pulling from the wall, which is the
    figure RAPL cannot reach: it covers the GPU, the disks, the fans and the PSU's
    own losses, not just the CPU package. ``ipmitool dcmi power reading`` (or DCMI
    over the local KCS interface) answers in watts, an instantaneous rate rather
    than a counter, so an implementation multiplies by ``elapsed_seconds`` for the
    interval's joules and reports ``is_floor=False``.

    Needs a BMC, so it is server hardware only -- there is nothing to talk to on a
    laptop or a consumer desktop -- plus ``ipmitool`` and access to the IPMI device
    nodes. ``sample`` answering None keeps a node that lists this backend falling
    through to the next one.
    """

    name = "ipmi"

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        return None


class NvmlBackend:
    """Per-GPU draw from NVIDIA's management library. Unimplemented.

    ``nvmlDeviceGetPowerUsage`` (the library behind ``nvidia-smi``) reports each
    GPU's draw in milliwatts. RAPL never sees the GPU at all, so on a machine that
    rents out CUDA work the package figure misses the largest consumer in the box.

    Unlike the others this is an *addend*, not an alternative: a GPU reading is not
    a substitute for the CPU reading. Wiring it into :func:`first_reading`, where
    the first answer wins, would report the GPU and drop the package. Summing
    backends is a different composition than falling through them, and the monitor
    does not do it, which is why ``sample`` answers None.
    """

    name = "nvml"

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        return None


class SmartPlugBackend:
    """A metering plug between the machine and the wall. Unimplemented.

    A Shelly or Tasmota plug publishes instantaneous watts, and usually a
    cumulative watt-hour counter, over HTTP or MQTT. It is the only source that
    measures the socket itself, so it is the one figure that needs no caveat about
    what it leaves out -- the accurate option for a home node, and the reason the
    others are described as floors.

    The costs are real: the operator has to buy the plug and put its address in
    config, the poll leaves the host over the network, and the plug measures
    whatever is plugged into it, which may be a power strip holding more than this
    machine. ``sample`` answers None until there is something configured to poll.
    """

    name = "smart_plug"

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        return None


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
