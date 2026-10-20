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
from typing import Dict, Mapping, Optional

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
    # No period is the kernel's default period (Sysresources.cpu_period), not "no
    # CPU": billing 0 vCPUs for a quota with no period gave the CPU away.
    from src.utils.cost_functions.resource_availability import _DEFAULT_CPU_PERIOD_US

    period = int(getattr(system_resources, "cpu_period", 0) or 0) or _DEFAULT_CPU_PERIOD_US
    quota = int(getattr(system_resources, "cpu_quota", 0) or 0)

    vcpus = Decimal(quota) / Decimal(period) if quota > 0 else Decimal(0)
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
    except (psutil.Error, OSError) as e:
        # Treat an unreadable machine as exhausted rather than free: the failure mode of
        # over-charging is a refused client, and of under-charging is giving the node
        # away. Only one of those is recoverable.
        #
        # OSError as well as psutil.Error, and they are unrelated types: `disk_usage`
        # raises the former (a missing or unreadable mount point), and this runs inside
        # the manager loop, which does not guard its calls -- an escaping exception here
        # kills the thread that charges every instance.
        logger(f"[PRICING] Could not read system load ({e}); pricing as fully scarce.")
        scarcity = {resource: 1.0 for resource in RESOURCES}

    _scarcity_cache, _scarcity_cached_at = scarcity, now
    return scarcity


def scarcity_bp(lack_of_supply: float, price_vector: Optional[Prices] = None) -> int:
    """Scarcity surcharge as basis points: 10_000 is 1x, i.e. no surcharge.

    Runs from 1x when the resource is plentiful to SCARCITY_MAX_MULTIPLIER when it is
    gone. SCARCITY_CURVE shapes the approach: 1.0 is linear, higher values stay near 1x
    until the resource is genuinely scarce and then climb steeply.

    The exponent is the curve itself, not its reciprocal: at curve 2.0 a tenth of the
    supply gone costs 1.09x, not 3.85x. Reciprocating it made the surcharge arrive
    *earlier* as the curve rose, the opposite of what every description of the setting
    says, and only the linear case (1.0, where the two agree) had a test.
    """
    p = price_vector or prices()
    lack = max(0.0, min(float(lack_of_supply), 1.0))
    shaped = lack ** p.scarcity_curve
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


def launch_price_bp(scarcity: Optional[Dict[str, float]] = None) -> Dict[str, int]:
    """The multiplier each resource was priced at, in basis points, for a launch now.

    This is what `local_instances` records for an instance, and what the bound of
    `pricing.MAX_PRICE_VOLATILITY` is measured from. A launch while the node is free
    (`is_free`) records 0 for every resource: the instance was admitted for nothing, and
    0 times any bound is 0, so it stays free for as long as it runs.

    The multiplier is recorded, not an amount of MU, because an instance can be resized
    (`ModifyServiceSystemResources`) and an amount would then price the old shape.
    """
    current = scarcity if scarcity is not None else system_scarcity()
    if is_free(current):
        return {resource: 0 for resource in RESOURCES}
    p = prices()
    return {resource: scarcity_bp(current.get(resource, 1.0), p) for resource in RESOURCES}


def _bounded_bp(current_bp: int, launch_bp: Optional[int], p: Prices) -> int:
    """`current_bp`, held down to the ceiling that `launch_bp` allows.

    Ceiling only, not symmetric: the bound limits how far the price may RISE above the
    launch price, and a fall passes through whole. A symmetric bound would also keep a
    price up while the node empties, so a client would pay more than the current load
    asks for -- the case where the node is idle and the client has no reason to stay.
    The risk the bound answers is the other one: a rise that the funding of the
    instance did not cover, which stops it and costs the client reputation (#323).

    No bound set, or no launch price known (NULL on a row from before the column
    existed), leaves `current_bp` as it is.
    """
    bound = p.volatility_bound()
    if bound is None or launch_bp is None:
        return current_bp
    ceiling = int(Decimal(int(launch_bp)) * Decimal(str(bound)))
    return min(current_bp, ceiling)


def maintenance_charge_mu(
    system_resources: celaut.Sysresources,
    seconds: float,
    scarcity: Optional[Dict[str, float]] = None,
    arch: Optional[str] = None,
    launch_bp: Optional[Mapping[str, int]] = None,
) -> int:
    """MU owed for holding the requested resources for `seconds`.

    This is the recurring charge: the manager calls it once per iteration with that
    iteration's length, so the price of an hour is the same however often the node ticks.

    ``arch`` is the guest's architecture, which selects the memory price when the
    operator has set one per arch (`pricing.BY_ARCH.<arch>.RAM_MU_PER_GIB_HOUR`).
    Omitted -- or naming an arch with no price of its own -- charges the node's scalar
    memory price, so a caller that does not know the arch bills the ordinary rate
    rather than nothing.

    ``launch_bp`` is the price the instance was launched at (`launch_price_bp`). With
    `pricing.MAX_PRICE_VOLATILITY` on, each resource's multiplier is held to that
    price times the bound. Omitted, the charge is unbounded. This clamp lives here, and
    not in the maintenance loop, so that every caller that prices a running instance
    gets it -- see `worst_case_charge_mu` for the quote.
    """
    current = scarcity if scarcity is not None else system_scarcity()
    if is_free(current):
        return 0

    p = prices()
    bps = {
        resource: _bounded_bp(
            scarcity_bp(current.get(resource, 1.0), p),
            None if launch_bp is None else launch_bp.get(resource),
            p,
        )
        for resource in RESOURCES
    }
    return _charge_at(system_resources, seconds, bps, arch, p)


def worst_case_charge_mu(
    system_resources: celaut.Sysresources,
    seconds: float,
    scarcity: Optional[Dict[str, float]] = None,
    arch: Optional[str] = None,
) -> int:
    """The most `maintenance_charge_mu` can levy for an instance launched now.

    The instance is priced at the launch price of this moment, and then at the highest
    multiplier its bound allows. A quote of the maximum maintenance cost and the funding
    of the initial balance both use this, so they cannot disagree with the ticks that
    spend the balance: no tick of such an instance can exceed this figure.

    With `pricing.MAX_PRICE_VOLATILITY` off there is no worst case that the node
    promises, and this is the charge at the current load, as it was before the bound
    existed.
    """
    current = scarcity if scarcity is not None else system_scarcity()
    if is_free(current):
        return 0

    p = prices()
    launch = launch_price_bp(current)
    top = p.scarcity_max_multiplier * SCARCITY_SCALE
    bps = {
        resource: (
            scarcity_bp(current.get(resource, 1.0), p)
            if p.volatility_bound() is None
            else _bounded_bp(top, launch[resource], p)
        )
        for resource in RESOURCES
    }
    return _charge_at(system_resources, seconds, bps, arch, p)


def _charge_at(
    system_resources: celaut.Sysresources,
    seconds: float,
    bps: Mapping[str, int],
    arch: Optional[str],
    p: Prices,
) -> int:
    """MU for holding the resources for `seconds`, at one multiplier (bp) per resource."""
    units = requested_units(system_resources)
    return sum(
        (
            per_time_charge(p.ram_for_arch(arch), units[MEM], seconds, bps[MEM]),
            per_time_charge(p.cpu_mu_per_vcpu_hour, units[CPU], seconds, bps[CPU]),
            per_time_charge(p.disk_mu_per_gib_hour, units[DISK], seconds, bps[DISK]),
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


# There is deliberately no `start_charge_mu`. Starting an instance costs the one-off
# build and nothing else: the runtime window is priced exactly once, as the balance the
# instance starts with (`manager.default_initial_balance`), which the maintenance ticks
# then spend. Charging occupancy for that same window here as well billed it twice --
# the client paid for two hours and got one. See compute_start_service_cost.
