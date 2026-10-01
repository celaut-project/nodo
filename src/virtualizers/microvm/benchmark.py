"""Booting this node's own guest to measure what one of its cores can do (#452).

The measurement is `/init`'s ``nodo.benchmark=1`` branch (bash/build_ch_initramfs.sh):
the pinned guest kernel and initramfs every service boots, with no rootfs, one vCPU,
four timed busybox loops, tagged lines on the serial console, and a power-off. This
module boots that guest under the hypervisor the node would run a service of that
architecture under -- Cloud Hypervisor for the host's own, QEMU+TCG for each foreign
one emulation is enabled for -- reads the lines back, and writes the scores to the
cache `src.utils.node_benchmark` owns. Admission reads only that cache, so nothing
here is on the admission path; that is also why this module may import as much of
the microVM family as it likes and `resource_availability` must never import it.

When it runs:

* **at daemon start**, in a background thread (`start_background_refresh`), and only
  for an architecture whose cache entry no longer describes this node -- see
  :func:`fingerprint`. A node restarted with nothing changed boots nothing;
* **on `nodo benchmark`**, which measures every served architecture again whatever
  the cache says.

A guest that never finishes, prints the initramfs's fatal line, or exits without
"done" is a failed measurement: logged, the process stopped, and the cache left as
it was. An entry whose fingerprint no longer matches is dropped *before* measuring,
so a failure there leaves that architecture unmeasured -- unknown to admission --
rather than holding services to numbers that describe another kernel or hypervisor.
"""
import fcntl
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.utils import logger as log, min_benchmark, node_benchmark
from src.utils.arch_guard import host_arch_tag
from src.utils.config import ConfigManager
from src.virtualizers.microvm import guest as microvm_guest
from src.virtualizers.microvm import initramfs as microvm_initramfs
from src.virtualizers.microvm import serial
from src.virtualizers.qemu import config as qemu_config
from src.virtualizers.registry import CH, QEMU

env_manager = ConfigManager()

BENCHMARK_CMDLINE_TOKEN = "nodo.benchmark=1"

# One vCPU, because every primitive is per core. The RAM only has to hold the
# unpacked initramfs and dd's 64 MiB block with room to spare.
BENCHMARK_VCPUS = 1
BENCHMARK_MEM_MIB = 512

# The guest measures for ~10 s. The rest is boot, which under TCG is slow; a guest
# still silent after this long is not going to finish.
BOOT_TIMEOUT_S = 300.0

# How long a guest that printed "done" gets to power itself off before it is stopped.
DONE_GRACE_S = 5.0

POLL_INTERVAL_S = 0.5

SERIAL_LOG_NAME = "benchmark.serial.log"


class BenchmarkFailed(RuntimeError):
    """A benchmark boot that produced no usable measurement, and why."""


@dataclass(frozen=True)
class Target:
    """One architecture this node serves, and how it would boot a guest of it."""

    arch: str
    virtualizer: str
    binary: str
    kernel: str
    initramfs: str


def _ch_binary() -> Optional[str]:
    configured = env_manager.get("virtualizers.ch.BINARY_PATH")
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    return shutil.which("cloud-hypervisor")


def _asset(key: str, arch: str) -> str:
    return str((env_manager.get(key) or {}).get(arch) or "")


def served_targets() -> List[Target]:
    """Every architecture a service could be launched under here, as it would be.

    The host's own under CH, when the binary, kernel and initramfs are all there --
    which is `selection.select_virtualizer`'s rule -- and each foreign one QEMU is
    ready for. Nothing else: admission only ever asks about what this node runs.
    """
    targets: List[Target] = []
    host = host_arch_tag()
    if host:
        binary = _ch_binary()
        kernel = _asset("virtualizers.ch.KERNEL_PATHS", host)
        initramfs = _asset("virtualizers.ch.INITRAMFS_PATHS", host)
        if binary and os.path.isfile(kernel) and os.path.isfile(initramfs):
            targets.append(Target(host, CH, binary, kernel, initramfs))

    for arch in sorted(qemu_config.QEMU_SYSTEM_BINARIES):
        if arch == host or not qemu_config.emulation_ready(arch):
            continue
        targets.append(Target(
            arch, QEMU,
            str(qemu_config.qemu_system_binary(arch)),
            str(qemu_config.qemu_kernel_path(arch)),
            str(qemu_config.qemu_initramfs_path(arch)),
        ))
    return targets


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key.strip() in ("model name", "CPU part", "Hardware"):
                    return value.strip()
    except OSError:
        pass
    return platform.processor()


def fingerprint(target: Target) -> str:
    """What a measurement of ``target`` depends on, as one digest.

    The guest kernel and initramfs by content (a re-installed guest is a different
    guest even at the same path), the hypervisor binary by path, size and mtime (an
    upgrade replaces the file), and the host's kernel release and CPU model (KVM is
    the host kernel's, and a disk moved to another machine keeps its cache). Any of
    them changing is a cache that no longer describes this node.
    """
    binary = os.stat(target.binary)
    parts = [
        f"format={node_benchmark.CACHE_FORMAT}",
        f"capability={microvm_initramfs.BENCHMARK_CAPABILITY}",
        f"arch={target.arch}",
        f"virtualizer={target.virtualizer}",
        f"binary={os.path.realpath(target.binary)}:{binary.st_size}:{binary.st_mtime_ns}",
        f"kernel={_sha256_file(target.kernel)}",
        f"initramfs={_sha256_file(target.initramfs)}",
        f"host_kernel={platform.release()}",
        f"cpu={_cpu_model()}",
    ]
    if target.virtualizer == QEMU:
        parts.append(f"qemu_cpu={env_manager.get('virtualizers.qemu.CPU_MODEL', 'max')}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def ch_command(target: Target, serial_log: Path) -> List[str]:
    """Cloud Hypervisor, booting the guest with no disk, no network and one vCPU.

    No ``--disk``: the benchmark branch runs before /init looks for one. No ``--net``:
    nothing is reachable from it, and no TAP has to be created and torn down. No API
    socket and no renamed process either -- this VM has no runtime state, so there is
    nothing for the janitor to sweep and nothing a recycled PID could impersonate.
    """
    return [
        target.binary,
        "--kernel", target.kernel,
        "--initramfs", target.initramfs,
        "--cpus", f"boot={BENCHMARK_VCPUS}",
        "--memory", f"size={BENCHMARK_MEM_MIB}M",
        "--cmdline", f"console={microvm_guest.serial_device()} {BENCHMARK_CMDLINE_TOKEN}",
        "--serial", f"file={serial_log}",
        "--console", "off",
    ]


def qemu_command(target: Target, serial_log: Path) -> List[str]:
    """QEMU+TCG, the way a service of a foreign architecture is booted, minus its I/O.

    ``-no-reboot`` turns anything other than a power-off into an exit as well, so a
    guest that panics ends the wait instead of rebooting into a second benchmark.
    """
    return [
        target.binary,
        "-machine", qemu_config.QEMU_MACHINE_BY_ARCH.get(target.arch, "q35"),
        "-accel", "tcg",
        "-cpu", str(env_manager.get("virtualizers.qemu.CPU_MODEL", "max") or "max"),
        "-smp", str(BENCHMARK_VCPUS),
        "-m", f"{BENCHMARK_MEM_MIB}M",
        "-kernel", target.kernel,
        "-initrd", target.initramfs,
        "-append",
        f"console={qemu_config.QEMU_CONSOLE_BY_ARCH.get(target.arch, 'ttyS0')} {BENCHMARK_CMDLINE_TOKEN}",
        "-nic", "none",
        "-display", "none",
        "-monitor", "none",
        "-serial", f"file:{serial_log}",
        "-no-reboot",
    ]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_for_benchmark(
        process: subprocess.Popen,
        serial_log: Path,
        *,
        timeout: float = BOOT_TIMEOUT_S,
        done_grace: float = DONE_GRACE_S,
        poll_interval: float = POLL_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Wait for a benchmark guest to finish, and return its serial log.

    New ground for the node: a service guest never exits, so every other wait asks
    "is it up yet?". This one asks "is it over?", three ways, whichever comes first:

    * the hypervisor exited -- the guest powered off, which is the normal end;
    * the log says ``done`` -- finished, but could not power off; it gets
      ``done_grace`` to do so and is then stopped;
    * the initramfs printed its fatal line -- an /init that does not know
      ``nodo.benchmark=1`` and went looking for a rootfs. It will never finish.

    Raises :class:`BenchmarkFailed` on a timeout, on the fatal line, and on an exit
    with no ``done`` (a crash, or a guest that panicked). The process is never left
    running, whichever way this returns.
    """
    deadline = clock() + timeout
    done_at: Optional[float] = None
    try:
        while True:
            exited = process.poll() is not None
            text = _read(serial_log)
            if exited:
                if node_benchmark.DONE_LINE in text:
                    return text
                raise BenchmarkFailed(
                    f"the hypervisor exited with code {process.returncode} before the "
                    f"guest finished; serial tail: {serial.tail_file(serial_log, 10)}"
                )
            if done_at is None and node_benchmark.DONE_LINE in text:
                done_at = clock()
            if done_at is not None and clock() - done_at >= done_grace:
                return text
            fatal = serial.detect_initramfs_fatal(serial_log)
            if fatal:
                raise BenchmarkFailed(f"the guest's initramfs gave up: {fatal}")
            if clock() >= deadline:
                raise BenchmarkFailed(
                    f"no result within {timeout:.0f}s; serial tail: "
                    f"{serial.tail_file(serial_log, 10)}"
                )
            sleep(poll_interval)
    finally:
        _stop(process)


def measure(target: Target, *, timeout: float = BOOT_TIMEOUT_S) -> Dict[str, int]:
    """Boot one benchmark guest for ``target`` and return what it measured.

    Only primitives this node can name are returned: a score for a key admission
    cannot compare is a score nothing reads. Raises :class:`BenchmarkFailed`.
    """
    try:
        capability = microvm_initramfs.benchmark_capability(target.initramfs)
    except microvm_initramfs.InitramfsReadError as e:
        raise BenchmarkFailed(f"cannot read the initramfs at {target.initramfs}: {e}") from e
    if capability != microvm_initramfs.BENCHMARK_CAPABILITY:
        raise BenchmarkFailed(
            f"the initramfs at {target.initramfs} has no benchmark branch "
            f"(marker benchmark:{capability or '<absent>'}, this node needs "
            f"{microvm_initramfs.BENCHMARK_CAPABILITY}); re-run the installer once a "
            "guest with it is published"
        )

    workdir = Path(tempfile.mkdtemp(prefix="nodo-benchmark-"))
    try:
        serial_log = workdir / SERIAL_LOG_NAME
        command = ch_command(target, serial_log) if target.virtualizer == CH \
            else qemu_command(target, serial_log)
        log.LOGGER(f"[benchmark][{target.arch}] booting: {' '.join(command)}")
        with open(workdir / "stderr.log", "w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr)
        text = wait_for_benchmark(process, serial_log, timeout=timeout)
        scores = {
            key: value
            for key, value in node_benchmark.parse_benchmark_serial_output(text).items()
            if key in min_benchmark.MIN_BENCHMARK_KEYS
        }
        if not scores:
            raise BenchmarkFailed(
                f"the guest finished but reported no scores; serial tail: "
                f"{serial.tail_file(serial_log, 10)}"
            )
        return scores
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _lock_path() -> Optional[str]:
    cache = node_benchmark.default_cache_path()
    return f"{cache}.lock" if cache else None


def refresh(*, force: bool = False, timeout: float = BOOT_TIMEOUT_S) -> Dict[str, str]:
    """Measure every served architecture whose cache entry is stale (all, if ``force``).

    Returns ``{arch: outcome}`` for the caller to print. Never raises for one
    architecture's failure: each is measured, recorded or logged on its own. One
    measurement at a time per node -- a second caller (`nodo benchmark` while the
    daemon is starting) gets ``{}`` back rather than a second VM fighting the first
    for the core it is timing.
    """
    lock_path = _lock_path()
    if not lock_path:
        log.LOGGER("[benchmark] main.CACHE is not configured; not measuring.")
        return {}
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.LOGGER("[benchmark] another measurement is already running; not starting a second.")
            return {}

        targets = served_targets()
        cached = node_benchmark.read_node_benchmark_cache()
        # An architecture this node no longer serves (emulation switched off, assets
        # removed) keeps no scores: nothing will be admitted under it to compare.
        node_benchmark.forget_node_benchmark(set(cached) - {t.arch for t in targets})

        outcomes: Dict[str, str] = {}
        for target in targets:
            prefix = f"[benchmark][{target.arch}][{target.virtualizer}]"
            try:
                current = fingerprint(target)
            except OSError as e:
                outcomes[target.arch] = f"skipped: cannot fingerprint the guest ({e})"
                log.LOGGER(f"{prefix} {outcomes[target.arch]}")
                continue
            entry = cached.get(target.arch) or {}
            if entry.get("fingerprint") == current and entry.get("scores") and not force:
                outcomes[target.arch] = "unchanged: " + min_benchmark.describe(entry["scores"])
                log.LOGGER(f"{prefix} {outcomes[target.arch]}")
                continue
            try:
                if entry and entry.get("fingerprint") != current:
                    node_benchmark.forget_node_benchmark([target.arch])
                    log.LOGGER(f"{prefix} guest, hypervisor or host changed; dropped the old scores.")
                scores = measure(target, timeout=timeout)
                node_benchmark.write_node_benchmark_cache(
                    scores, arch=target.arch, virtualizer=target.virtualizer, fingerprint=current,
                )
            except (BenchmarkFailed, OSError, ValueError) as e:
                outcomes[target.arch] = f"failed: {e}"
                log.LOGGER(f"{prefix} measurement failed, cache left as it was: {e}")
                continue
            outcomes[target.arch] = "measured: " + min_benchmark.describe(scores)
            log.LOGGER(f"{prefix} {outcomes[target.arch]}")
        return outcomes


def start_background_refresh() -> threading.Thread:
    """Run :func:`refresh` on a daemon thread, at node start.

    Off the startup path entirely: a benchmark boot takes seconds under KVM and
    minutes under TCG, and nothing about serving should wait for it, or fail with it.
    Until it finishes admission simply has no scores, which is what a node that has
    never measured looks like anyway.
    """
    def _run() -> None:
        try:
            refresh()
        except Exception as e:
            log.LOGGER(f"[benchmark] background measurement raised, ignored: {type(e).__name__}: {e}")

    thread = threading.Thread(target=_run, name="nodo-benchmark", daemon=True)
    thread.start()
    return thread
