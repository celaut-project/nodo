"""``nodo benchmark`` -- measure this node's cores again, now, and show the result.

The daemon measures at start, and only what changed since the last measurement (see
`src.virtualizers.microvm.benchmark`). This is the operator's way to ask for it
regardless: after a BIOS change the fingerprint cannot see, to check what a
`min_benchmark` will be held against, or to retry a measurement that failed.
``--show`` prints the cache without booting anything.
"""
import time
from typing import List

from src.utils import min_benchmark, node_benchmark


def _print_cache() -> None:
    entries = node_benchmark.read_node_benchmark_cache()
    print(f"Cache: {node_benchmark.default_cache_path() or '<main.CACHE is not configured>'}", flush=True)
    if not entries:
        print("No scores measured yet: a declared min_benchmark is logged, not enforced.", flush=True)
        return
    for arch in sorted(entries):
        entry = entries[arch]
        measured_at = entry.get("measured_at")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(measured_at)) \
            if isinstance(measured_at, int) else "unknown"
        print(f"  {arch} ({entry.get('virtualizer', '?')}, measured {when}):", flush=True)
        for key in sorted(entry.get("scores") or {}):
            print(f"    {key} = {entry['scores'][key]}", flush=True)
        unmeasured = [k for k in min_benchmark.MIN_BENCHMARK_KEYS if k not in (entry.get("scores") or {})]
        if unmeasured:
            print(f"    not measured (never enforced): {', '.join(unmeasured)}", flush=True)


def benchmark_command(args: List[str]) -> bool:
    """Run it. False when nothing could be measured, so the shell sees a failure."""
    if "--show" in args:
        _print_cache()
        return True

    from src.virtualizers.microvm import benchmark

    print("Booting this node's guest with nodo.benchmark=1 for each architecture it serves...", flush=True)
    outcomes = benchmark.refresh(force=True)
    if not outcomes:
        print(
            "Nothing measured: no architecture is servable here, main.CACHE is unset, "
            "or another measurement is already running (see the node log).",
            flush=True,
        )
        return False
    for arch in sorted(outcomes):
        print(f"  {arch}: {outcomes[arch]}", flush=True)
    print("", flush=True)
    _print_cache()
    return any(outcome.startswith("measured") for outcome in outcomes.values())
