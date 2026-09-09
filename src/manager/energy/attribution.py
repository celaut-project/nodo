"""Split node watts across running local instances by CPU share.

RAPL is package energy, so CPU share is the honest axis. RAM/disk/GPU are not
in the RAPL figure and must not be used to invent a more precise-looking split.

The split is against **the machine's** CPU, never against the other instances.
An instance that used a twentieth of one core out of eight caused a twentieth of
one core's worth of watts whether it is alone on the host or one of twenty; a
denominator built from the other instances would hand the only guest on a busy
build machine the entire load term. Whatever no instance accounts for -- the
host's own work, nodo itself, the operator's shell, and idle draw -- stays in
``other``.

The model backend is idle + load. Idle watts stay unattributed; only the load
term is split.

Delegated instances are not in the weight map: their draw happens on the
owning peer.

This module is pure math — no nodo imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Tuple


@dataclass(frozen=True)
class Attribution:
    """Per-instance watts and share of *node* watts, plus the unattributed rest."""

    instance_watts: Dict[str, float]
    instance_share: Dict[str, float]
    other_watts: float
    other_share: float


def _shares(watts: Mapping[str, float], node_watts: float) -> Dict[str, float]:
    if node_watts <= 0:
        return {key: 0.0 for key in watts}
    return {key: value / node_watts for key, value in watts.items()}


def attribute_load(
    load_watts: float,
    weights: Mapping[str, float],
    busy_cores: float,
) -> Tuple[Dict[str, float], float]:
    """Split ``load_watts`` over the host's busy CPU, by non-negative CPU weights.

    ``busy_cores`` is how many cores' worth of CPU the whole machine used over the
    interval (``cpu_percent / 100 * cpu_count``), and it is the denominator. An
    instance's watts are therefore its own doing and nothing else's: they do not
    move when a second guest starts, and they do not swallow the host's share.

    The denominator is floored at the summed weights, because the two figures come
    from different clocks -- cgroup counters per instance, a host-wide percentage
    for the machine -- and a skew that puts the instances above the host total
    would otherwise attribute more watts than the interval holds.

    Returns ``(instance_watts, unattributed_load)``. Zero busy CPU, or no weight
    at all, leaves the whole load unattributed rather than splitting it evenly:
    power nobody can be shown to have caused belongs to the host.
    """
    clean = {key: max(0.0, float(weight)) for key, weight in weights.items()}
    total = sum(clean.values())
    denominator = max(total, max(0.0, float(busy_cores)))
    if load_watts <= 0 or denominator <= 0:
        return {key: 0.0 for key in clean}, max(0.0, float(load_watts))
    instance_watts = {
        key: load_watts * (weight / denominator) for key, weight in clean.items()
    }
    unattributed = max(0.0, load_watts - sum(instance_watts.values()))
    return instance_watts, unattributed


def attribute(
    node_watts: float,
    weights: Mapping[str, float],
    busy_cores: float,
    idle_watts: float = 0.0,
) -> Attribution:
    """Attribute ``node_watts`` across instances.

    ``busy_cores`` is the host's CPU use over the interval, in cores; see
    :func:`attribute_load` for why it and not the instances is the denominator.

    ``idle_watts`` is the portion that is never given to instances (model idle,
    or 0 for RAPL). It is clamped to ``[0, node_watts]``.
    """
    node = max(0.0, float(node_watts))
    idle = min(node, max(0.0, float(idle_watts)))
    load = node - idle
    instance_watts, unattributed_load = attribute_load(load, weights, busy_cores)
    other = idle + unattributed_load
    return Attribution(
        instance_watts=instance_watts,
        instance_share=_shares(instance_watts, node),
        other_watts=other,
        other_share=(other / node) if node > 0 else 1.0,
    )
