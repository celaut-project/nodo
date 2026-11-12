"""What the devices of an instance cost, and the power they draw (#536, phase P6).

The price of a device is flat: ``devices.BY_CHIP.<chip>.mu_per_second``, with no
scarcity surcharge (version 1). It is part of the maintenance of the instance:

* a quote (``GetServiceEstimatedCost``) adds the rate of the devices that the node
  would give to ``init_maintenance_cost`` and ``max_maintenance_cost``, so the
  funding of the instance (``price_bound.funding_sum_mu``) covers them, and the
  father's overcharge check (#567) holds the ticks to that figure;
* each reservation records the rate of each device (``device_assignments``), and
  the first tick records the sum as the launch rate of the instance
  (``local_instances.launch_device_mu_per_second``). A tick charges the rate of the
  devices held now, held to ``launch rate * (1 + v)^k`` when the instance has a price
  bound (#636), like the other resources. A hibernated instance holds no device and
  pays none;
* the ``maintenance`` movement says the rate in its detail
  (``device_mu_per_second``).

The node cannot read the power of a device bound to vfio-pci (no host driver), so
an instance is attributed the ``watts`` configured for its chips.
"""
from typing import Dict, List, Mapping, Optional, Sequence

from protos import celaut_pb2
from src.devices import admission, assignments, inventory, spec

RATE_PREFIX = "device_mu_per_second:"


def entry_rate(entry: spec.NodeDevice) -> int:
    return inventory.chip_price_mu_per_second(entry.chip) or 0


def quote_rate(service: Optional[celaut_pb2.Service]) -> int:
    """MU per second of the devices that the node would give to ``service`` now."""
    if not admission.has_devices(service) or not inventory.enabled():
        return 0
    asked = list(service.container.devices)
    found, _ = spec.assign(asked, inventory.free_devices())
    if found is None:
        # None free now: the price of the devices that it would get once they are.
        found, _ = spec.assign(asked, inventory.node_devices())
    return sum(entry_rate(e) for e in found or [])


def held_rate(instance_id: str) -> int:
    """MU per second of the devices that the instance holds now (recorded rates)."""
    return sum(int(row.get("mu_per_second") or 0) for row in assignments.of_instance(instance_id))


def tick_rate(instance_id: str, launch_rate: Optional[int], bound=None,
              elapsed_seconds: Optional[float] = None) -> int:
    """The rate that a tick charges: the held rate, held to the bound of the launch."""
    rate = held_rate(instance_id)
    if rate <= 0 or bound is None or launch_rate is None:
        return rate
    k = bound.period_index(elapsed_seconds)
    factor = bound.factor(k)
    if factor is None:
        return rate
    return min(rate, int(int(launch_rate) * factor))


def charge_mu(rate: int, seconds: float) -> int:
    return int(max(0, int(rate)) * max(0.0, float(seconds)))


def advertised_rates() -> Dict[str, int]:
    """``device_mu_per_second:<chip>`` for each chip that the node offers."""
    if not inventory.enabled():
        return {}
    rates: Dict[str, int] = {}
    for entry in inventory.node_devices(with_assignments=False):
        rate = entry_rate(entry)
        if rate > 0:
            rates[f"{RATE_PREFIX}{entry.chip}"] = rate
    return rates


def device_watts() -> Dict[str, float]:
    """``{instance id: watts}`` of the devices that each instance holds."""
    out: Dict[str, float] = {}
    held = assignments.assigned()
    if not held:
        return out
    chips: Dict[str, str] = {}
    for instance_id in set(held.values()):
        for row in assignments.of_instance(instance_id):
            chips[row["address"]] = row.get("chip") or ""
    for address, instance_id in held.items():
        watts = inventory.chip_settings(chips.get(address, "")).get("watts")
        if isinstance(watts, (int, float)) and not isinstance(watts, bool) and watts > 0:
            out[instance_id] = out.get(instance_id, 0.0) + float(watts)
    return out
