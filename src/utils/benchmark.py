"""`Sysresources.benchmark`: the minimum per-core throughput a service requires.

`cpu_quota / cpu_period` says how many cores a service needs and nothing about how fast
one is: the same admitted 1.0 core is native silicon on one host and software emulation
on another. `benchmark` is where a service states the second half, as key/value
entries from a named primitive to the least it needs of it **per core, per second** -- so it composes
with the quota ("2 cores, each at least 500k int ops/s") instead of competing with it.

This module is the request side only: the vocabulary, and reading a declaration out of
`service.json`. Nothing here measures a host, and no node publishes a score yet.
"""
from typing import Any, Dict, Final, Mapping, Tuple

from src.utils import keyvalue

# The primitives this node knows by name, every one of them per core and per second.
# Adding a key here is all it takes to extend the set: the field is keyed precisely so
# a new primitive is never a wire-format change. A key that is *not* here is still
# carried -- a peer may know a primitive this node does not -- so this is what the node
# can name, not a whitelist.
BENCHMARK_KEYS: Final[Tuple[str, ...]] = (
    "int_ops_per_sec",              # integer/branch-heavy operations
    "flt_ops_per_sec",              # floating-point operations
    "mem_bandwidth_bytes_per_sec",  # bytes of memory read + written
    "sha256_hashes_per_sec",        # SHA-256 digests
)

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
    return tuple(sorted(key for key in benchmark if key not in BENCHMARK_KEYS))


def describe(benchmark: Mapping[str, int]) -> str:
    """``key=value`` for every declared primitive, sorted, for a log line."""
    return ", ".join(f"{key}={benchmark[key]}" for key in sorted(benchmark))
