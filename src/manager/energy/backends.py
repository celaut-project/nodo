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


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def package_domain_dirs(root: Path) -> List[Path]:
    """Package-level RAPL domains under ``root``, as their ``name`` file declares.

    A control type holds one directory per domain, named after itself: ``<ct>:0``
    is a top-level domain and ``<ct>:0:0`` a subdomain of it (dram, core, uncore)
    whose energy the package total already carries, so counting a subdomain would
    count it twice. Depth is read from the number of colons rather than a hardcoded
    prefix, since the control type is named after the driver -- ``intel-rapl`` even
    on AMD.

    Which top-level domain is a *package* cannot be inferred from its position:
    ``<ct>:1`` is the second socket on a two-socket board, but on a client CPU it
    is commonly ``psys``, the whole-platform domain, which *contains* the package
    instead of sitting beside it. Summing the two would report the platform twice
    over. So the ``name`` file decides and only ``package*`` is taken.
    """
    if not root.is_dir():
        return []
    packages: List[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for path in sorted(entries, key=lambda p: p.name):
        if len(path.name.split(":")) != 2:
            continue
        if not path.is_dir():
            continue
        name = _read_text(path / "name")
        if not name or not name.startswith("package"):
            continue
        packages.append(path)
    return packages


def read_package_counters(root: Path) -> Dict[str, Tuple[int, int]]:
    """``(energy_uj, max_energy_range_uj)`` per package domain, keyed by directory.

    Kept per domain rather than summed, because each counter wraps on its own
    ``max_energy_range_uj``: a total tells you a wrap happened somewhere but not in
    which domain, and the range to add back is the wrapping domain's alone.

    A domain whose ``energy_uj`` cannot be read is left out. One with no readable
    range gets 0, which :func:`wrapped_delta` reads as "a wrap here cannot be
    interpreted".
    """
    counters: Dict[str, Tuple[int, int]] = {}
    for domain in package_domain_dirs(root):
        energy = _read_int(domain / "energy_uj")
        if energy is None:
            continue
        rng = _read_int(domain / "max_energy_range_uj")
        counters[domain.name] = (energy, rng if rng and rng > 0 else 0)
    return counters


def package_power_limit_watts(root: Path = RAPL_ROOT) -> Optional[float]:
    """Sustained power limit the CPU packages declare, in watts, or None.

    Each domain carries its constraints as ``constraint_<n>_name`` and
    ``constraint_<n>_power_limit_uw``; the one named ``long_term`` is the package's
    sustained ceiling, near enough its TDP. On a two-socket board the limits add up
    the way the counters do.

    Worth having because of an asymmetry in the permissions: ``energy_uj`` is
    root-only on current kernels, while the constraint files are world-readable. A
    node that cannot learn what it is drawing can still learn what it is allowed to
    draw, which is a real per-machine number where the alternative is a guess.

    It is a *package* ceiling, so it says nothing about RAM, disks, fans or the
    power supply's losses, and nothing about idle draw either.
    """
    total = 0.0
    for domain in package_domain_dirs(root):
        limit_uw = None
        for constraint in sorted(domain.glob("constraint_*_name")):
            if _read_text(constraint) != "long_term":
                continue
            prefix = constraint.name[: -len("_name")]
            limit_uw = _read_int(domain / f"{prefix}_power_limit_uw")
            break
        if limit_uw is None or limit_uw <= 0:
            return None
        total += limit_uw / 1_000_000.0
    return total if total > 0 else None


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
    """CPU-package RAPL. Readings are a floor, not wall-socket watts.

    Holds the previous counter of every package domain, so a two-socket board is
    two deltas added together and a wrap is resolved against the range of the
    domain that wrapped.
    """

    name = "rapl"

    def __init__(self, root: Path = RAPL_ROOT):
        self.root = Path(root)
        self._previous_uj: Optional[Dict[str, int]] = None

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        counters = read_package_counters(self.root)
        if not counters:
            return None
        previous = self._previous_uj
        self._previous_uj = {
            domain: energy for domain, (energy, _) in counters.items()
        }
        if previous is None or elapsed_seconds <= 0:
            return None
        total_uj = 0
        matched = 0
        for domain, (current_uj, max_range_uj) in counters.items():
            before = previous.get(domain)
            if before is None:
                # A domain that appeared mid-run has no delta of its own yet.
                continue
            delta_uj = wrapped_delta(before, current_uj, max_range_uj)
            if delta_uj is None:
                # An uninterpretable wrap makes the whole interval unmeasurable:
                # reporting the other domains alone would understate the package
                # total and read as a drop in draw that did not happen.
                return None
            total_uj += delta_uj
            matched += 1
        if matched == 0:
            return None
        joules = total_uj / MICROJOULES_PER_JOULE
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
    """Idle + linear-in-utilisation estimator, for machines with no counter.

    Two coefficients define a straight line: ``idle_watts`` at 0% CPU and
    ``idle_watts + load_watts`` at 100%. Both are **calibration inputs**, not
    tuning knobs -- they come from a meter at the wall, read once with the machine
    quiet and once with every core busy. Guessing them is not a smaller version of
    measuring them: a figure that suits a mid-range desktop is an order of
    magnitude out on a Raspberry Pi and half of what a two-socket server idles at.

    ``idle_watts`` of 0 means uncalibrated, and ``sample`` then answers None so the
    node reports nothing rather than a number nobody measured. Idle is the
    coefficient that has to come from a human: the load term can be taken from the
    package's own declared ceiling (:func:`package_power_limit_watts`), but no
    machine publishes what it burns doing nothing.

    Even calibrated, the line only meets the curve at its two ends. Power goes with
    V²f and voltage climbs with frequency, so the last points of utilisation cost
    far more watts than the first; ``cpu_percent`` averages over cores, so four
    cores pinned and eight half-busy read the same; and RAM, disk and GPU are not
    in the formula at all. It is an order of magnitude, not a measurement, which is
    what ``is_floor=False`` and the TUI's "model estimate" label say.

    ``cpu_percent_fn`` must be non-blocking, so the manager thread never sleeps in
    here for an interval.
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
