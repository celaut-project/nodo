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
from typing import Any, Dict, Final, List, Mapping, Optional, Tuple

from src.utils import keyvalue

# The primitives this node knows by name, every one of them per core and per second.
# Adding a key here is all it takes to extend the set: the field is keyed precisely so
# a new primitive is never a wire-format change. A key that is *not* here is still
# carried -- a peer may know a primitive this node does not -- so this is what the node
# can name, not a whitelist.
BENCHMARK_KEYS: Final[Tuple[str, ...]] = (
    "int_ops_per_sec",              # integer/branch-heavy operations
    "flt_ops_per_sec",              # floating-point operations
    "mem_bandwidth_bytes_per_sec",  # bytes of memory written, over a working set
    "sha256_hashes_per_sec",        # SHA-256 digests
)

MEM_BANDWIDTH_KEY: Final[str] = "mem_bandwidth_bytes_per_sec"

# Not a primitive: the amount of memory MEM_BANDWIDTH_KEY was (or must be) measured
# over, travelling in the same keyed field. A bandwidth is only comparable with another
# taken over the same working set -- 5 KiB lives in L1 and 2 GiB does not -- and without
# the size a node could report its cache and call it memory.
MEM_WORKING_SET_KEY: Final[str] = "mem_bandwidth_working_set_bytes"

# The working set a requirement that names none is read against: 1 GiB. Larger than any
# last-level cache one core can use (the largest unified LLCs ship at ~0.5 GiB) and half
# the memory the benchmark service runs with. celaut-basics/demo-service's benchmark
# pins the same number (benchmark/bench.sh, DEFAULT_WORKING_SET_BYTES).
DEFAULT_MEM_BANDWIDTH_WORKING_SET_BYTES: Final[int] = 1 << 30

# Every key a node's score for one architecture holds in config.yaml.
SCORE_KEYS: Final[Tuple[str, ...]] = BENCHMARK_KEYS + (MEM_WORKING_SET_KEY,)

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

    An unrecognised *key* is kept, not refused; see :data:`BENCHMARK_KEYS`. The
    result is ordered by key, so what is logged about a declaration does not depend on
    the order its author happened to write it in.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"service.json {path} must be an object.")
    keyvalue.check_json_object(value, path)

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
    return tuple(sorted(key for key in benchmark if key not in SCORE_KEYS))


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
      required -- or, for memory bandwidth, when it was measured over a smaller working
      set than the one required, or over none at all: a smaller set can only flatter a
      bandwidth, and a score that does not say its set cannot be compared with anything;
    * **unenforced**, when there is no score to hold it against (unmeasured here, or a
      primitive this node has no name for). An unknown capacity is not evidence of an
      insufficient one.

    A requirement on memory bandwidth that names no working set is read against
    :data:`DEFAULT_MEM_BANDWIDTH_WORKING_SET_BYTES`. The working-set key on its own,
    with no bandwidth required, requires nothing.
    """
    found: List[str] = []
    unenforced: Dict[str, int] = {}
    for key in sorted(required):
        if key == MEM_WORKING_SET_KEY:
            continue
        minimum = required[key]
        if key not in BENCHMARK_KEYS:
            unenforced[key] = minimum
            continue
        score = scores.get(key, UNMEASURED)
        if score is None or score < 0:
            unenforced[key] = minimum
            continue
        if key == MEM_BANDWIDTH_KEY:
            wanted_set = required.get(MEM_WORKING_SET_KEY)
            if wanted_set is None:
                wanted_set = DEFAULT_MEM_BANDWIDTH_WORKING_SET_BYTES
            measured_set = scores.get(MEM_WORKING_SET_KEY, UNMEASURED)
            if measured_set is None or measured_set < 0:
                found.append(
                    f"resources.at_init.benchmark.{key} cannot be checked: the score "
                    f"{where} names no {MEM_WORKING_SET_KEY}, and a bandwidth measured "
                    "over an unknown amount of memory is never accepted."
                )
                continue
            if measured_set < wanted_set:
                found.append(
                    f"resources.at_init.benchmark.{key} needs a working set of at least "
                    f"{wanted_set} bytes; the score {where} was measured over "
                    f"{measured_set} bytes, which a cache can flatter."
                )
                continue
        if score < minimum:
            found.append(
                f"Insufficient per-core resources.at_init.benchmark.{key}. "
                f"Requested: {minimum} per core per second, measured {where}: {score}."
            )
    return found, unenforced
