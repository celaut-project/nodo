"""What this node measured about its own cores, and holding a `min_benchmark` against it.

`src.utils.min_benchmark` is the request side of #448: a service saying how fast a core
it needs. This is the other half (#452): the scores this node measured for itself, kept
where admission can read them, and the comparison between the two.

The measuring is done elsewhere, by booting the node's own pinned guest with
``nodo.benchmark=1`` (`src.virtualizers.microvm.benchmark`). It is kept out of here
on purpose: admission (`resource_availability`) must not import a virtualizer, nor
boot anything, to answer "does this shape fit?". So admission only ever reads the
cache this module owns, and everything here is stdlib apart from where the cache
lives -- which is the node's config, read only when a path is not passed in.

Scores are kept per architecture. A node serves its own architecture under Cloud
Hypervisor and, where emulation is enabled, foreign ones under QEMU+TCG -- the same
"1.0 core" an order of magnitude apart, which is the whole reason `min_benchmark`
exists. A score is therefore only ever held against a service of the architecture
it was measured for.
"""
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, Final, List, Mapping, Optional

from src.utils.arch_guard import host_arch_tag
from src.utils.min_benchmark import MIN_BENCHMARK_KEYS

# What the guest's /init prints, one primitive per line: `[nodo-benchmark] key=value`.
# Anchored to the start of a line, so a `set -x` trace of the echo (`+ echo
# '[nodo-benchmark] ...'`) or a kernel line that happens to quote one is not read
# as a second, different measurement.
BENCHMARK_TAG: Final[str] = "[nodo-benchmark]"
_LINE_RE: Final = re.compile(r"^\[nodo-benchmark\] ([a-z0-9_]+)=([0-9]+)\s*$", re.MULTILINE)

# Printed by /init after the last primitive. Not a score: what tells the node the
# guest finished, when the guest cannot power itself off.
DONE_LINE: Final[str] = f"{BENCHMARK_TAG} done"

CACHE_FILE_NAME: Final[str] = "node_benchmark.json"

# Bumped when an entry's shape changes. A cache in any other format is read as
# empty -- unmeasured, which admission treats as unknown -- rather than guessed at.
CACHE_FORMAT: Final[int] = 1

_UINT64_MAX: Final[int] = 2 ** 64 - 1


def parse_benchmark_serial_output(text: str) -> Dict[str, int]:
    """Every ``[nodo-benchmark] key=value`` line in a serial log, as ``{key: value}``.

    Unrecognised keys are kept: which primitives count is admission's business, not
    the parser's, and a newer initramfs measuring a primitive this node has no name
    for yet should not read as a broken log. A key printed twice keeps its last value
    (a serial log can hold two boots). ``\\r`` is tolerated because some consoles
    write CRLF.
    """
    scores: Dict[str, int] = {}
    for key, value in _LINE_RE.findall((text or "").replace("\r", "")):
        number = int(value)
        if number <= _UINT64_MAX:
            scores[key] = number
    return scores


def default_cache_path() -> Optional[str]:
    """``<main.CACHE>/node_benchmark.json``, or ``None`` when no cache is configured.

    The config is imported here rather than at module scope so that the parser and
    the comparison stay usable without one.
    """
    try:
        from src.utils.config import ConfigManager

        cache = ConfigManager().get("CACHE")
    except Exception:
        return None
    if not cache:
        return None
    return os.path.join(str(cache), CACHE_FILE_NAME)


def _clean_scores(raw: Any) -> Dict[str, int]:
    """Only ``{non-empty str: non-negative int}`` survives; ``bool`` is not a score."""
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and key
        and isinstance(value, int) and not isinstance(value, bool)
        and 0 <= value <= _UINT64_MAX
    }


def read_node_benchmark_cache(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """The whole cache, ``{arch: entry}``, each entry with its scores cleaned.

    Never raises. A missing, unreadable, malformed or other-format file is ``{}``: on
    the admission path a broken cache must cost the enforcement, never the answer.
    """
    path = path or default_cache_path()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("format") != CACHE_FORMAT:
        return {}
    archs = data.get("archs")
    if not isinstance(archs, dict):
        return {}

    entries: Dict[str, Dict[str, Any]] = {}
    for arch, entry in archs.items():
        if not isinstance(arch, str) or not isinstance(entry, dict):
            continue
        entries[arch] = {**entry, "scores": _clean_scores(entry.get("scores"))}
    return entries


def get_cached_node_benchmark(arch: Optional[str] = None, path: Optional[str] = None) -> Dict[str, int]:
    """The scores this node measured for ``arch`` (the host's when omitted), or ``{}``.

    Never raises, never measures. ``{}`` means "not measured", which every caller has
    to read as unknown and not as zero.
    """
    try:
        arch = arch or host_arch_tag()
    except Exception:
        return {}
    if not arch:
        return {}
    entry = read_node_benchmark_cache(path).get(arch) or {}
    return dict(entry.get("scores") or {})


def _write_entries(entries: Mapping[str, Mapping[str, Any]], path: str) -> None:
    """Replace the cache with ``entries``, atomically.

    A temporary file in the same directory and ``os.replace``: admission reads this
    file while a measurement may be writing it, and must see the old cache or the new
    one, never half of either.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".node_benchmark.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"format": CACHE_FORMAT, "archs": dict(entries)}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_node_benchmark_cache(
        scores: Mapping[str, int],
        *,
        arch: str,
        virtualizer: str,
        fingerprint: str = "",
        path: Optional[str] = None,
) -> None:
    """Record ``scores`` as what ``arch`` measured under ``virtualizer``.

    Other architectures' entries are kept. ``fingerprint`` says what was measured --
    guest kernel, initramfs, hypervisor, host -- so the measuring side can tell a
    cache that still describes this node from one that does not; admission never
    reads it. Raises ``OSError`` when the file cannot be written, and ``ValueError``
    when no cache path is configured: the caller decides whether that is worth a log
    line or a failed command.
    """
    path = path or default_cache_path()
    if not path:
        raise ValueError("main.CACHE is not configured; nowhere to keep benchmark scores.")
    entries = read_node_benchmark_cache(path)
    entries[arch] = {
        "virtualizer": virtualizer,
        "fingerprint": fingerprint,
        "measured_at": int(time.time()),
        "scores": _clean_scores(dict(scores)),
    }
    _write_entries(entries, path)


def forget_node_benchmark(archs, path: Optional[str] = None) -> None:
    """Drop the entries for ``archs``, so they read as unmeasured. Raises like the writer."""
    path = path or default_cache_path()
    if not path:
        return
    entries = read_node_benchmark_cache(path)
    kept = {arch: entry for arch, entry in entries.items() if arch not in set(archs)}
    if kept != entries:
        _write_entries(kept, path)


def benchmark_shortfalls(declared: Mapping[str, int], measured: Mapping[str, int]) -> List[str]:
    """One reason per primitive this node measured below what ``declared`` requires.

    Only a primitive that is both recognised (`MIN_BENCHMARK_KEYS`) and measured can
    fall short. Anything else is silent, by the same rule as an uncountable
    `cpu_total`: an unknown capacity is not evidence of an insufficient one. A
    declared primitive this node cannot name, or did not manage to measure, must not
    refuse a service an older node -- which never measured anything -- would admit.
    """
    shortfalls: List[str] = []
    for key in sorted(declared):
        if key not in MIN_BENCHMARK_KEYS or key not in measured:
            continue
        required, have = int(declared[key]), int(measured[key])
        if have < required:
            shortfalls.append(
                f"Insufficient per-core benchmark for resources.at_most.min_benchmark.{key}. "
                f"Requested: {required} per core per second, measured on this node: {have}."
            )
    return shortfalls
