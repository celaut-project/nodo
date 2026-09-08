"""Read per-instance CPU from cgroup v2 ``cpu.stat``.

The TUI already does this for the live CPU% column. The energy sampler needs
the same number on the Python side at sample time, without scraping the TUI
and without a blocking ``psutil`` interval.

``usage_usec`` is cumulative core-microseconds. Two snapshots yield a weight
in cores (1.0 = one core busy for the whole interval). Missing files or a
first snapshot produce no weight — the instance is then omitted from the
split rather than treated as idle-and-therefore-owed-a-share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


DEFAULT_CGROUPS_BASE = Path("/sys/fs/cgroup")


def instance_cgroup_dir(instance_id: str, base: Path = DEFAULT_CGROUPS_BASE) -> Path:
    safe_id = str(instance_id or "").strip()
    if not safe_id or "/" in safe_id or safe_id in (".", ".."):
        raise ValueError(f"Invalid instance id for cgroup path: {instance_id!r}")
    return Path(base) / "nodo-ch" / safe_id


def read_usage_usec(cgroup_dir: Path) -> Optional[int]:
    """``usage_usec`` from ``cpu.stat``, or None if unreadable."""
    path = Path(cgroup_dir) / "cpu.stat"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "usage_usec":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


@dataclass
class CpuWeightTracker:
    """Delta ``usage_usec`` across samples into a cores-used weight."""

    _previous: Dict[str, int] = field(default_factory=dict)

    def weights(
        self,
        usage_usec: Dict[str, Optional[int]],
        elapsed_seconds: float,
    ) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if elapsed_seconds <= 0:
            self._remember(usage_usec)
            return out
        elapsed_usec = elapsed_seconds * 1_000_000.0
        for instance_id, current in usage_usec.items():
            previous = self._previous.get(instance_id)
            if current is None or previous is None:
                continue
            delta = current - previous
            if delta < 0:
                # Counter reset (cgroup recycled). Skip this interval.
                continue
            out[instance_id] = delta / elapsed_usec
        self._remember(usage_usec)
        return out

    def _remember(self, usage_usec: Dict[str, Optional[int]]) -> None:
        seen = set()
        for instance_id, current in usage_usec.items():
            seen.add(instance_id)
            if current is not None:
                self._previous[instance_id] = current
        for stale in list(self._previous):
            if stale not in seen:
                self._previous.pop(stale, None)
