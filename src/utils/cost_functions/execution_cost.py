"""What an instance costs, per resource.

The model this replaced collapsed CPU, memory and disk into a single availability
scalar and multiplied it by one flat `EXECUTION_COST`, so an 8 GiB instance and a
128 MiB instance paid nearly the same, and a node could not say "memory is scarce here
but disk is not". Here each resource carries its own price and its own scarcity
surcharge, and the charge grows with what is actually held.

A note on what was silently broken: the old code read `cpu_limit` and `disk_limit` off
`Sysresources` through `getattr(..., None)`. Those fields do not exist on the message --
it carries `cpu_period` / `cpu_quota` (CFS) and `disk_space` -- so both always read as
absent and only memory ever influenced a price. CPU and disk are billed here for the
first time.

Amounts are MU (see src/utils/monetary.py). Nothing here deals in ERG or in floats:
scarcity is the only fractional quantity and it travels as integer basis points.
"""
import time
from decimal import Decimal
from typing import Dict, Optional

import psutil

from protos import celaut_pb2 as celaut
from src.utils.logger import LOGGER as logger
from src.utils.monetary import (
    SCARCITY_SCALE,
    Prices,
    bytes_to_gib,
    free_tier,
    per_time_charge,
    per_volume_charge,
    prices,
)
from src.utils.utils import read_service_from_disk
from src.utils.verify import get_service_hex_main_hash
from src.virtualizers.architecture import UnsupportedArchitectureException, check_supported_architecture
from src.virtualizers.interface import is_built

CPU = "cpu"
MEM = "mem"
DISK = "disk"
RESOURCES = (CPU, MEM, DISK)

# Scarcity is sampled from the whole machine, so every caller within a tick wants the
# same answer. Re-reading it per instance would both cost syscalls and let two instances
# in one tick be priced against different readings.
_SCARCITY_TTL_SECONDS = 1.0
_scarcity_cache: Optional[Dict[str, float]] = None
_scarcity_cached_at: float = 0.0


def requested_units(system_resources: celaut.Sysresources) -> Dict[str, Decimal]:
    """What an instance asks for, in the units its prices are quoted in.

    Memory and disk in GiB; CPU in vCPUs, derived from the CFS pair the way the
    hypervisor reads it (quota/period: 200000/100000 is two cores).
    """
    mem_bytes = int(getattr(system_resources, "mem_limit", 0) or 0)
    disk_bytes = int(getattr(system_resources, "disk_space", 0) or 0)
    period = int(getattr(system_resources, "cpu_period", 0) or 0)
    quota = int(getattr(system_resources, "cpu_quota", 0) or 0)

    vcpus = Decimal(quota) / Decimal(period) if period > 0 and quota > 0 else Decimal(0)
    return {
        MEM: bytes_to_gib(mem_bytes),
        DISK: bytes_to_gib(disk_bytes),
        CPU: vcpus,
    }


def system_scarcity(force_refresh: bool = False) -> Dict[str, float]:
    """How much of each resource is missing: 0.0 plentiful, 1.0 exhausted.

    Per resource, never averaged together -- that separation is the whole point.

    CPU is sampled non-blockingly (usage since the previous call), so the very first
    reading after start reports an idle machine. The manager re-reads every iteration,
    so it self-corrects within one tick, and that is preferable to blocking the caller
    for a sampling interval on every price quote.
    """
    global _scarcity_cache, _scarcity_cached_at

    now = time.monotonic()
    if not force_refresh and _scarcity_cache is not None and (now - _scarcity_cached_at) < _SCARCITY_TTL_SECONDS:
        return _scarcity_cache

    try:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        scarcity = {
            CPU: max(0.0, min(psutil.cpu_percent(interval=None) / 100.0, 1.0)),
            MEM: 1.0 - (memory.available / memory.total if memory.total else 0.0),
            DISK: 1.0 - (disk.free / disk.total if disk.total else 0.0),
        }
    except psutil.Error as e:
        # Treat an unreadable machine as exhausted rather than free: the failure mode of
        # over-charging is a refused client, and of under-charging is giving the node
        # away. Only one of those is recoverable.
        logger(f"[PRICING] Could not read system load ({e}); pricing as fully scarce.")
        scarcity = {resource: 1.0 for resource in RESOURCES}

    _scarcity_cache, _scarcity_cached_at = scarcity, now
    return scarcity


def scarcity_bp(lack_of_supply: float, price_vector: Optional[Prices] = None) -> int:
    """Scarcity surcharge as basis points: 10_000 is 1x, i.e. no surcharge.

    Runs from 1x when the resource is plentiful to SCARCITY_MAX_MULTIPLIER when it is
    gone. SCARCITY_CURVE shapes the approach: 1.0 is linear, higher values stay near 1x
    until the resource is genuinely scarce and then climb steeply.
    """
    p = price_vector or prices()
    lack = max(0.0, min(float(lack_of_supply), 1.0))
    shaped = lack ** (1.0 / p.scarcity_curve)
    multiplier = 1.0 + (p.scarcity_max_multiplier - 1) * shaped
    return int(round(multiplier * SCARCITY_SCALE))


def is_free(scarcity: Optional[Dict[str, float]] = None) -> bool:
    """True while the node is giving its capacity away.

    `free_tier.FREE_WHILE_SCARCITY_BELOW` is a load threshold, not an allowance: every
    resource has to be below it. One busy resource is enough to start charging, because
    that is the one the next client will contend for.
    """
    threshold = free_tier().free_while_scarcity_below
    if threshold <= 0:
        return False
    current = scarcity if scarcity is not None else system_scarcity()
    return all(current.get(resource, 1.0) < threshold for resource in RESOURCES)


def maintenance_charge_mu(
    system_resources: celaut.Sysresources,
    seconds: float,
    scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """MU owed for holding the requested resources for `seconds`.

    This is the recurring charge: the manager calls it once per iteration with that
    iteration's length, so the price of an hour is the same however often the node ticks.
    """
    current = scarcity if scarcity is not None else system_scarcity()
    if is_free(current):
        return 0

    p = prices()
    units = requested_units(system_resources)
    return sum(
        (
            per_time_charge(p.ram_mu_per_gib_hour, units[MEM], seconds, scarcity_bp(current.get(MEM, 1.0), p)),
            per_time_charge(p.cpu_mu_per_vcpu_hour, units[CPU], seconds, scarcity_bp(current.get(CPU, 1.0), p)),
            per_time_charge(p.disk_mu_per_gib_hour, units[DISK], seconds, scarcity_bp(current.get(DISK, 1.0), p)),
        )
    )


def traffic_charge_mu(num_bytes: int) -> int:
    """MU owed for relaying `num_bytes`. Volume, not time, so no scarcity surcharge."""
    return per_volume_charge(prices().net_mu_per_gib, num_bytes)


def build_charge_mu(metadata: celaut.Metadata) -> int:
    """MU owed for building this service's container, or 0 if it is already built."""
    try:
        service_hash = get_service_hex_main_hash(metadata=metadata)
        if is_built(service_hash):
            return 0

        logger(f"System has no built container to run service {service_hash}.")

        if not check_supported_architecture(
            service=read_service_from_disk(service_hash=service_hash),
            metadata=metadata,
        ):
            raise UnsupportedArchitectureException(arch=str(metadata))

        return prices().build_mu
    except UnsupportedArchitectureException:
        raise
    except Exception as e:
        logger(f"[PRICING] Build charge failed: {e}")
        raise


def start_charge_mu(metadata: celaut.Metadata, system_resources: celaut.Sysresources, seconds: float) -> int:
    """MU owed to start an instance: the one-off build, plus `seconds` of occupancy.

    The occupancy part is what funds the instance until the first maintenance tick; the
    caller decides how long it is buying (`deposits.INITIAL_RUNTIME_HOURS`).
    """
    if is_free():
        return 0
    return build_charge_mu(metadata=metadata) + maintenance_charge_mu(
        system_resources=system_resources, seconds=seconds
    )
