"""Split node watts across running local instances by CPU share.

RAPL is package energy, so CPU share is the honest axis. RAM/disk/GPU are not
in the RAPL figure and must not be used to invent a more precise-looking split.

If no instance is using CPU, nothing is attributed: idle package power is the
host, not "every instance equally". Inventing an equal split would claim
instances caused power they did not.

The model backend is idle + load. Idle watts stay unattributed (``other``);
only the load term is split.

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
) -> Tuple[Dict[str, float], float]:
    """Split ``load_watts`` by non-negative CPU weights.

    Returns ``(instance_watts, unattributed_load)``. Zero total weight leaves
    the whole load unattributed rather than splitting it evenly.
    """
    clean = {key: max(0.0, float(weight)) for key, weight in weights.items()}
    total = sum(clean.values())
    if load_watts <= 0 or total <= 0:
        return {key: 0.0 for key in clean}, max(0.0, float(load_watts))
    instance_watts = {
        key: load_watts * (weight / total) for key, weight in clean.items()
    }
    return instance_watts, 0.0


def attribute(
    node_watts: float,
    weights: Mapping[str, float],
    idle_watts: float = 0.0,
) -> Attribution:
    """Attribute ``node_watts`` across instances.

    ``idle_watts`` is the portion that is never given to instances (model idle,
    or 0 for RAPL). It is clamped to ``[0, node_watts]``.
    """
    node = max(0.0, float(node_watts))
    idle = min(node, max(0.0, float(idle_watts)))
    load = node - idle
    instance_watts, unattributed_load = attribute_load(load, weights)
    other = idle + unattributed_load
    return Attribution(
        instance_watts=instance_watts,
        instance_share=_shares(instance_watts, node),
        other_watts=other,
        other_share=(other / node) if node > 0 else 1.0,
    )
