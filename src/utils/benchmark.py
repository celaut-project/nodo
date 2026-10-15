"""`Sysresources.benchmark`: per-core throughput, required by a service or measured by a node.

`cpu_quota / cpu_period` says how many cores a service needs and nothing about how fast
one is: the same admitted 1.0 core is native silicon on one host and software emulation
on another. `benchmark` is the second half, as key/value entries from a named primitive
to an amount of it **per core, per second** -- so it composes with the quota ("2 cores,
each at least 500k int ops/s") instead of competing with it.

The same keyed field means two things depending on where it sits (see its comment in
`protos/celaut.proto`): in `resources.at_init` of a service, the minimum it requires; in
a `Peer`'s announced `ArchitectureResources`, what that node measured. This module holds
the vocabulary, the `service.json` parser, this node's own scores (`config.yaml`,
`benchmark.BY_ARCH`) and the comparison of a requirement against a score.

Nothing here measures anything or imports a virtualizer: admission reads config through
it and nothing else. Measuring is the optional `benchmark` core service's job
(:mod:`src.core_services.benchmark`), and an operator may write the scores by hand.
"""
import re
from typing import Any, Dict, Final, List, Mapping, Optional, Tuple

from src.utils import keyvalue

# Memory bandwidth is only comparable with another taken over the same amount of memory
# -- 5 KiB lives in L1 and 2 GiB does not -- and without the size a node could report its
# cache and call it memory. So the size is part of the *name*: one primitive per working
# set, ``mem_bandwidth_<size>_bytes_per_sec``. A node *measures* the sizes below; a
# service may *require* any size (see :func:`shortfalls`). The sizes are the ones the benchmark service can run:
# its guest has 2 GiB, so 1 GiB is the largest set it measures. Adding a size is adding
# an entry here (and having the service measure it).
MEM_WORKING_SETS: Final[Mapping[str, int]] = {
    "64mib": 64 << 20,
    "256mib": 256 << 20,
    "1gib": 1 << 30,
}

# key -> the working set (bytes) that bandwidth is measured, and required, over.
MEM_BANDWIDTH_WORKING_SET: Final[Mapping[str, int]] = {
    f"mem_bandwidth_{size}_bytes_per_sec": nbytes for size, nbytes in MEM_WORKING_SETS.items()
}
MEM_BANDWIDTH_KEYS: Final[Tuple[str, ...]] = tuple(MEM_BANDWIDTH_WORKING_SET)

# A requirement may name *any* working set, not only the ones this node measures:
# ``mem_bandwidth_<n><kib|mib|gib>_bytes_per_sec``. It is read against the score over the
# smallest measured working set that is at least as large (see :func:`shortfalls`).
_MEM_BANDWIDTH_KEY: Final = re.compile(r"^mem_bandwidth_([1-9][0-9]*)(kib|mib|gib)_bytes_per_sec$")
_SIZE_UNITS: Final[Mapping[str, int]] = {"kib": 1 << 10, "mib": 1 << 20, "gib": 1 << 30}


def mem_bandwidth_working_set(key: str) -> Optional[int]:
    """The working set (bytes) a ``mem_bandwidth_<size>_bytes_per_sec`` key names, else None."""
    match = _MEM_BANDWIDTH_KEY.match(key)
    return int(match.group(1)) * _SIZE_UNITS[match.group(2)] if match else None


def is_recognised(key: str) -> bool:
    """Whether this node can name ``key``: a primitive it knows, or a bandwidth over some size."""
    return key in SCORE_KEYS or mem_bandwidth_working_set(key) is not None

# The primitives this node knows by name, every one of them per core and per second.
# Adding a key here is all it takes to extend the set: the field is keyed precisely so
# a new primitive is never a wire-format change. A key that is *not* here is still
# carried -- a peer may know a primitive this node does not -- so this is what the node
# can name, not a whitelist.
BENCHMARK_KEYS: Final[Tuple[str, ...]] = (
    "int_ops_per_sec",              # integer/branch-heavy operations
    "flt_ops_per_sec",              # floating-point operations
    *MEM_BANDWIDTH_KEYS,            # bytes of memory written, one key per working set
    "sha256_hashes_per_sec",        # SHA-256 digests
)

# Every key a node's score for one architecture holds in config.yaml.
SCORE_KEYS: Final[Tuple[str, ...]] = BENCHMARK_KEYS

# Names this vocabulary used to have, and what replaced them. Refused at pack time and
# in config.yaml rather than carried as "an unrecognised key": a requirement the node
# would silently not enforce is a service admitted on hardware its author said it
# cannot run on.
REMOVED_KEYS: Final[Mapping[str, str]] = {
    "mem_bandwidth_bytes_per_sec": (
        "it was replaced by one key per working set: " + ", ".join(MEM_BANDWIDTH_KEYS)
    ),
    "mem_bandwidth_working_set_bytes": (
        "the working set is now part of the key: " + ", ".join(MEM_BANDWIDTH_KEYS)
    ),
}

# "Not measured" in config.yaml. Never compared against anything: a requirement on an
# unmeasured primitive is logged, not enforced, because an unknown capacity is not
# evidence of an insufficient one.
UNMEASURED: Final[int] = -1

# Where the scores live: `benchmark.BY_ARCH.<canonical arch>.<key>`, like
# `pricing.BY_ARCH`. Dotted on purpose -- ConfigManager.get falls back to searching
# every section for an undotted key, and `core_services.benchmark` would answer.
CONFIG_KEY: Final[str] = "benchmark.BY_ARCH"

_UINT64_MAX: Final[int] = 2 ** 64 - 1


def parse_benchmark(value: Any, path: str) -> Dict[str, int]:
    """A `service.json` ``benchmark`` object as ``{primitive: per-core minimum}``.

    Absent (``None``) is valid and is no requirement at all. A value has to be a
    non-negative integer that fits the wire's uint64: a requirement nobody can read is
    a packing error, because the two ways of "handling" one are dropping what the
    author asked for (read as 0) or a service no node will ever take (read as huge).
    ``bool`` is refused although Python counts it as an ``int`` -- ``true`` is a typo
    for a number here, never 1 op/s.

    An unrecognised *key* is kept, not refused; see :data:`BENCHMARK_KEYS`. A key this
    vocabulary used to have (:data:`REMOVED_KEYS`) is refused, naming its replacement. The
    result is ordered by key, so what is logged about a declaration does not depend on
    the order its author happened to write it in.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"service.json {path} must be an object.")
    keyvalue.check_json_object(value, path)
    for key in sorted(k for k in value if k in REMOVED_KEYS):
        raise ValueError(f"service.json {path}.{key} no longer exists: {REMOVED_KEYS[key]}.")

    parsed: Dict[str, int] = {}
    for key in sorted(value, key=str):
        if not isinstance(key, str) or not key:
            raise ValueError(f"service.json {path} keys must be non-empty strings.")
        minimum = value[key]
        if isinstance(minimum, bool) or not isinstance(minimum, int) \
                or not 0 <= minimum <= _UINT64_MAX:
            raise ValueError(
                f"service.json {path}.{key} must be a non-negative integer "
                f"(per core, per second), got {minimum!r}."
            )
        parsed[key] = minimum
    return parsed


def unrecognised_keys(benchmark: Mapping[str, int]) -> Tuple[str, ...]:
    """The keys of ``benchmark`` this node has no name for, sorted."""
    return tuple(sorted(key for key in benchmark if not is_recognised(key)))


def describe(benchmark: Mapping[str, int]) -> str:
    """``key=value`` for every declared primitive, sorted, for a log line."""
    return ", ".join(f"{key}={benchmark[key]}" for key in sorted(benchmark))


def node_scores(arch: Optional[str]) -> Dict[str, int]:
    """This node's scores for ``arch`` (a canonical tag) as written in config.yaml.

    Unmeasured keys are kept as :data:`UNMEASURED`; a key the config does not mention
    reads the same way. Never raises: a config that cannot be read is no scores at all,
    which admission treats as unmeasured -- the config validator is what refuses a
    malformed block, at load.
    """
    scores = {key: UNMEASURED for key in SCORE_KEYS}
    if not arch:
        return scores
    try:
        from src.utils.config import ConfigManager

        block = ConfigManager().get(CONFIG_KEY, {}) or {}
        entry = block.get(arch) if isinstance(block, dict) else None
    except Exception:
        return scores
    if not isinstance(entry, dict):
        return scores
    for key in SCORE_KEYS:
        value = entry.get(key, UNMEASURED)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            scores[key] = value
    return scores


def configured_architectures() -> Tuple[str, ...]:
    """The architectures config.yaml holds scores for, in the order written."""
    try:
        from src.utils.config import ConfigManager

        block = ConfigManager().get(CONFIG_KEY, {}) or {}
    except Exception:
        return ()
    return tuple(block) if isinstance(block, dict) else ()


def measured(scores: Mapping[str, int]) -> Dict[str, int]:
    """Only the measured entries of ``scores``, sorted by key: what a node may announce."""
    return {
        key: scores[key] for key in sorted(scores)
        if isinstance(scores[key], int) and scores[key] >= 0
    }


def unmeasured_keys(scores: Mapping[str, int]) -> Tuple[str, ...]:
    """The :data:`SCORE_KEYS` ``scores`` has no measurement for, in vocabulary order."""
    return tuple(key for key in SCORE_KEYS if scores.get(key, UNMEASURED) < 0)


def _score_for(key: str, scores: Mapping[str, int]) -> Tuple[str, Optional[int]]:
    """``(key, score)`` ``key`` is held against: its own, or for a bandwidth the next larger.

    A bandwidth whose own score is unmeasured falls back to the measured one over the
    smallest working set that is at least as large; ``(key, None)`` when there is none.
    """
    own = scores.get(key, UNMEASURED)
    wanted = mem_bandwidth_working_set(key)
    if wanted is None or (own is not None and own >= 0):
        return key, own
    larger = sorted(
        (size, other) for other, value in scores.items()
        if value is not None and value >= 0
        and (size := mem_bandwidth_working_set(other)) is not None and size >= wanted
    )
    return (larger[0][1], scores[larger[0][1]]) if larger else (key, None)


def shortfalls(
        required: Mapping[str, int],
        scores: Mapping[str, int],
        *,
        where: str,
) -> Tuple[List[str], Dict[str, int]]:
    """Hold a requirement against a score: ``(shortfalls, unenforced)``.

    ``required`` is a service's ``at_init.benchmark``; ``scores`` is a node's, either its
    own (:func:`node_scores`) or one a peer announced. ``where`` names the node and
    architecture in the messages ("on this node for linux/amd64").

    Each declared primitive ends up in exactly one of the two:

    * a **shortfall**, one message each, when the score is measured and below what is
      required;
    * **unenforced**, when there is no score to hold it against (unmeasured here, or a
      primitive this node has no name for). An unknown capacity is not evidence of an
      insufficient one.

    A memory bandwidth is only comparable over the same amount of memory, and a larger
    working set can only lower a bandwidth. So a requirement over a working set is held
    against the score under its own key when the node has one, and otherwise against the
    score over the **smallest larger working set** the node measured: that score is a
    safe floor for it. With no measured working set that large (or none at all) there is
    nothing to hold it against, and it is unenforced like any unmeasured primitive: a
    smaller set would flatter it, and an unknown capacity is not evidence of an
    insufficient one.
    """
    found: List[str] = []
    unenforced: Dict[str, int] = {}
    for key in sorted(required):
        minimum = required[key]
        if not is_recognised(key):
            unenforced[key] = minimum
            continue
        score_key, score = _score_for(key, scores)
        if score is None or score < 0:
            unenforced[key] = minimum
            continue
        if score < minimum:
            over = f" (over {score_key})" if score_key != key else ""
            found.append(
                f"Insufficient per-core resources.at_init.benchmark.{key}. "
                f"Requested: {minimum} per core per second, measured {where}: {score}{over}."
            )
    return found, unenforced
