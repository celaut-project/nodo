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

Four more sources measure what RAPL and the model cannot, each needing hardware
or configuration a node cannot be assumed to have, and each answering None until
it has it: ``smart_plug`` at the wall socket, ``ipmi`` at the power supply's
input, ``hwmon`` for a rail the kernel exposes, and ``nvml`` for the GPUs. They
are ordered by how much of the machine they see, most complete first, so a node
reports the best figure it has rather than the first one that exists.

``nvml`` is the exception to that order: a GPU's draw is not an alternative to
the CPU's, it adds to it. :func:`with_additions` handles that, and only for a
reading that is itself partial -- see there for why.

Everything outside sysfs costs wall time in the sampling tick: a subprocess for
``ipmi`` and ``nvml``, an HTTP request for ``smart_plug``. Every one of them
takes a hard timeout, and none is built at all unless configured, so a node that
configures none of them pays nothing. If the total ever grows past what the
manager loop can spare, one thread polling all three on their own cadence is the
shape to move to, not a longer timeout.

This module imports nothing from the rest of nodo so the math can be tested
without ``bee_rpc``. ``requests`` and the external commands are reached lazily,
so importing it costs neither.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple


RAPL_ROOT = Path("/sys/class/powercap/intel-rapl")
HWMON_ROOT = Path("/sys/class/hwmon")
MICROJOULES_PER_JOULE = 1_000_000.0
MICROWATTS_PER_WATT = 1_000_000.0
DEFAULT_EXTERNAL_TIMEOUT_SECONDS = 2.0


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


def reading_from_watts(
    watts: float,
    elapsed_seconds: float,
    backend: str,
    is_floor: bool,
) -> Optional[EnergyReading]:
    """An :class:`EnergyReading` from a source that answers in watts.

    A counter is integrated by subtraction; a rate has to be multiplied by the
    interval instead, which assumes the one figure held for the whole of it. That
    assumption is the price of a source that reports instantaneous power, and it
    is why an energy counter is preferred wherever a source offers both.

    Zero is not a reading. A machine that is running draws power, so a sensor
    answering 0 is not measuring this machine: a battery rail reads zero on
    mains, a plug reads zero with its socket switched off. Answering None lets
    the next source have it, where a 0 W sample would shadow every source behind
    it and claim the node draws nothing.
    """
    if elapsed_seconds <= 0:
        return None
    if watts is None or watts <= 0:
        return None
    return EnergyReading(
        joules=float(watts) * elapsed_seconds,
        watts=float(watts),
        backend=backend,
        is_floor=is_floor,
    )


def run_command(argv: Sequence[str], timeout_seconds: float) -> Optional[str]:
    """Standard output of ``argv``, or None if it cannot be run or fails.

    No shell: ``argv`` is passed as a list, so an operator's config value can
    never become a command. A missing binary, a non-zero exit and a timeout are
    all the same answer here -- this source cannot produce a reading -- and none
    of them raises.
    """
    if not argv or not shutil.which(argv[0]):
        return None
    try:
        finished = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if finished.returncode != 0:
        return None
    return finished.stdout


def http_get_json(url: str, timeout_seconds: float) -> Optional[Any]:
    """Parsed JSON from ``url``, or None if it cannot be fetched or parsed.

    Only http and https are accepted. `urllib` and `requests` both honour
    schemes like ``file:``, which would turn a mistyped config value into a
    local file read.
    """
    if not str(url).lower().startswith(("http://", "https://")):
        return None
    try:
        import requests

        response = requests.get(url, timeout=max(0.1, float(timeout_seconds)))
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def dig(payload: Any, path: str) -> Optional[Any]:
    """Follow a dotted ``path`` into parsed JSON, or None where it does not lead.

    Plugs disagree about where the number lives -- ``power`` on a Shelly Gen1,
    ``apower`` on a Gen2, ``StatusSNS.ENERGY.Power`` on Tasmota -- so config
    names the path instead of this module carrying a parser per vendor.
    """
    current = payload
    for key in str(path).split("."):
        if not key:
            return None
        if isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


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
        if watts <= 0:
            # See `reading_from_watts`: a live package does not draw nothing.
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
        if self.idle_watts <= 0:
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
    """A power or energy sensor the kernel publishes as hwmon.

    The reading for machines with no RAPL. On Apple Silicon under Asahi the
    ``macsmc`` drivers surface the SMC's rails here, which is the only power
    figure an M-series offers Linux; ARM boards with a shunt (INA219 and kin) and
    server boards exposing PMBus rails land in the same place.

    ``chip`` is the contents of a ``/sys/class/hwmon/hwmonN/name`` file and
    ``sensor`` is a prefix within it: ``power1`` reads ``power1_input``, in
    microwatts, a rate; ``energy1`` reads ``energy1_input``, in microjoules, a
    counter to subtract. Prefer an energy sensor where the chip offers one, since
    integrating a counter does not assume the rate held for the interval.

    Both have to come from config, because what a rail covers is a property of
    the board and is not discoverable: one machine's ``power1_input`` is the whole
    SoC, another's is a single 12V line, and on a laptop it is commonly the
    battery. Nothing here can pick the right sensor, and picking the wrong one
    silently reports a fraction of the machine as the machine.

    Readings are marked a floor for that reason -- a rail is a subset of what the
    socket sees. An `hwmonN` number is assigned at probe time and is not stable
    across reboots, so the chip is resolved by name, and re-resolved whenever the
    sensor stops reading.

    A laptop's battery is one of these chips, named for the battery itself, and
    it is a trap that looks like a whole-machine reading: it measures discharge,
    so on the mains a node is expected to run on it reads zero. `power_supply`'s
    ``power_now`` is the same sensor by another name. Zero is not treated as a
    reading anywhere here, so that misconfiguration falls through to the next
    source instead of publishing 0 W.
    """

    name = "hwmon"

    def __init__(self, chip: str, sensor: str, root: Path = HWMON_ROOT):
        self.chip = str(chip or "").strip()
        self.sensor = str(sensor or "").strip()
        self.root = Path(root)
        self._chip_dir: Optional[Path] = None
        self._previous_uj: Optional[int] = None

    def _resolve(self) -> Optional[Path]:
        if self._chip_dir is not None and (self._chip_dir / "name").is_file():
            return self._chip_dir
        self._chip_dir = None
        if not self.chip or not self.root.is_dir():
            return None
        try:
            entries = sorted(self.root.iterdir(), key=lambda entry: entry.name)
        except OSError:
            return None
        for entry in entries:
            if _read_text(entry / "name") == self.chip:
                self._chip_dir = entry
                return entry
        return None

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        if not self.sensor:
            return None
        chip_dir = self._resolve()
        if chip_dir is None:
            return None
        raw = _read_int(chip_dir / f"{self.sensor}_input")
        if raw is None:
            # The chip may have been renumbered; resolve again on the next tick.
            self._chip_dir = None
            return None
        if self.sensor.startswith("power"):
            return reading_from_watts(
                raw / MICROWATTS_PER_WATT, elapsed_seconds, self.name, is_floor=True
            )
        if not self.sensor.startswith("energy"):
            return None

        previous, self._previous_uj = self._previous_uj, raw
        if previous is None or elapsed_seconds <= 0:
            return None
        if raw < previous:
            # hwmon energy counters wrap with no range published anywhere, so a
            # decrease cannot be told from a reset. Neither is measurable.
            return None
        joules = (raw - previous) / MICROJOULES_PER_JOULE
        if joules <= 0:
            return None
        return EnergyReading(
            joules=joules,
            watts=joules / elapsed_seconds,
            backend=self.name,
            is_floor=True,
        )


class IpmiBackend:
    """Whole-machine draw from the board's management controller.

    A server's BMC reports the power the supply is pulling from the wall, which
    is the figure RAPL cannot reach: it covers the GPU, the disks, the fans and
    the supply's own losses, not just the CPU package. That makes it a reading of
    the machine rather than a floor, and the one an operator can compare against
    a bill.

    ``ipmitool dcmi power reading`` answers over the local KCS interface, in
    watts, an instantaneous rate. DCMI is the only interface asked for: reading a
    power sensor out of ``ipmitool sdr`` instead would mean knowing each vendor's
    name for it.

    Needs a BMC, so it is server hardware only -- there is nothing to talk to on
    a laptop or a consumer desktop -- plus ``ipmitool`` on PATH and access to the
    IPMI device. Any of those missing is None, not an error.
    """

    name = "ipmi"
    ARGV = ("ipmitool", "dcmi", "power", "reading")
    _READING = re.compile(r"instantaneous power reading\s*:\s*([0-9.]+)", re.IGNORECASE)

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_EXTERNAL_TIMEOUT_SECONDS,
        runner: Callable[[Sequence[str], float], Optional[str]] = run_command,
    ):
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        output = self._runner(self.ARGV, self.timeout_seconds)
        if not output:
            return None
        found = self._READING.search(output)
        if not found:
            return None
        try:
            watts = float(found.group(1))
        except ValueError:
            return None
        return reading_from_watts(watts, elapsed_seconds, self.name, is_floor=False)


class NvmlBackend:
    """Draw of the NVIDIA GPUs, summed.

    RAPL never sees the GPU, so on a machine renting out CUDA work the package
    figure misses the largest consumer in the box. `nvidia-smi` is asked rather
    than NVML through a binding, because the binary ships with the driver and a
    binding would be a dependency every node carries for the few that have a GPU.

    This is an *addend*, not an alternative: a GPU reading is not a substitute for
    the CPU's. :func:`with_additions` composes it, and :func:`first_reading`
    would be wrong for it -- the first answer wins there, so it would report the
    GPU and drop the package.

    A GPU that does not report power answers ``[N/A]`` and is skipped; a machine
    where the binary exists but the driver is not loaded exits non-zero, which is
    None. Both are ordinary on a host with a GPU that is not in use.
    """

    name = "nvml"
    ARGV = (
        "nvidia-smi",
        "--query-gpu=power.draw",
        "--format=csv,noheader,nounits",
    )

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_EXTERNAL_TIMEOUT_SECONDS,
        runner: Callable[[Sequence[str], float], Optional[str]] = run_command,
    ):
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        output = self._runner(self.ARGV, self.timeout_seconds)
        if not output:
            return None
        total = 0.0
        measured = 0
        for line in output.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                watts = float(text)
            except ValueError:
                continue  # [N/A], [Not Supported], a header from a future format
            if watts < 0:
                continue
            total += watts
            measured += 1
        if measured == 0:
            return None
        return reading_from_watts(total, elapsed_seconds, self.name, is_floor=True)


class SmartPlugBackend:
    """A metering plug between the machine and the wall.

    The only source that measures the socket itself, so it is the one figure that
    needs no caveat about what it leaves out -- the accurate option for a home
    node, and the reason the others are described as floors.

    ``url`` is the plug's own HTTP endpoint and ``power_path`` a dotted path to
    the watts inside the JSON it answers, since plugs disagree about where that
    lives:

    - Shelly Gen1: ``http://<ip>/meter/0`` with ``power``
    - Shelly Gen2/Plus: ``http://<ip>/rpc/Switch.GetStatus?id=0`` with ``apower``
    - Tasmota: ``http://<ip>/cm?cmnd=Status%208`` with
      ``StatusSNS.ENERGY.Power``

    Two things the operator owns and this cannot check: the plug measures
    whatever is plugged into it, which may be a strip holding more than this
    machine, and the request crosses the network in clear -- these devices speak
    plain HTTP on the LAN, and the reading is a statement about the household.
    """

    name = "smart_plug"

    def __init__(
        self,
        url: str,
        power_path: str = "power",
        timeout_seconds: float = DEFAULT_EXTERNAL_TIMEOUT_SECONDS,
        fetch: Callable[[str, float], Optional[Any]] = http_get_json,
    ):
        self.url = str(url or "").strip()
        self.power_path = str(power_path or "power").strip()
        self.timeout_seconds = float(timeout_seconds)
        self._fetch = fetch

    def sample(self, elapsed_seconds: float) -> Optional[EnergyReading]:
        if not self.url:
            return None
        payload = self._fetch(self.url, self.timeout_seconds)
        if payload is None:
            return None
        value = dig(payload, self.power_path)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            watts = float(value)
        except (TypeError, ValueError):
            return None
        return reading_from_watts(watts, elapsed_seconds, self.name, is_floor=False)


def with_additions(
    primary: Optional[EnergyReading],
    additive: Iterable[EnergyBackend],
    elapsed_seconds: float,
) -> Optional[EnergyReading]:
    """Add partial sources onto a reading that is itself partial.

    A floor reading measures a subset of the machine -- a CPU package, one rail --
    so a GPU's draw is missing from it and belongs added on. A reading that
    already covers the machine contains the GPU: a plug at the wall and the
    supply's own input both see it, and the model's coefficients were measured at
    the wall with it installed. Adding there would count it twice, so nothing is
    added and the additive sources are not even asked.

    The result keeps ``is_floor``, because a package plus its GPUs is still short
    of RAM, disks and the supply's losses. Its ``backend`` names every source
    that went into it, so a stored sample says what it was built from.
    """
    if primary is None or not primary.is_floor:
        return primary
    joules, watts, names = primary.joules, primary.watts, [primary.backend]
    for backend in additive:
        extra = backend.sample(elapsed_seconds)
        if extra is None:
            continue
        joules += extra.joules
        watts += extra.watts
        names.append(extra.backend)
    if len(names) == 1:
        return primary
    return replace(primary, joules=joules, watts=watts, backend="+".join(names))


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
