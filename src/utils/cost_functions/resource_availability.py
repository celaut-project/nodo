"""The local admission gate: "could this host run an instance shaped like this?".

One question with three callers -- ``launch_service``'s local preflight,
``generate_estimated_cost`` before it quotes, and the ``GetResourceAvailability`` RPC a
peer uses to evaluate a declared descendant workload -- so it lives on its own, needing
nothing but psutil and the memory pool. It is deliberately not part of
``generate_estimated_cost``, whose own imports reach ``src.utils.utils`` (netifaces) and
``src.virtualizers.interface`` (the CH build machinery, for unrelated billing helpers):
asking "does this shape fit?" should not pull in a virtualizer, nor require stubbing one
to test the answer.
"""
from typing import Any, Dict, Final, List, Tuple

import psutil

from protos import celaut_pb2 as celaut
from src.manager.resources import IOBigData, could_ve_this_sysreq


def _get_service_memory_snapshot() -> tuple[int, int]:
    # This is the same memory pool used by `could_ve_this_sysreq` through IOBigData.
    try:
        io_big_data = IOBigData()
        pool_total = int(io_big_data.ram_pool()) if callable(io_big_data.ram_pool) else 0
        pool_available = int(io_big_data.get_ram_avaliable()) if callable(io_big_data.get_ram_avaliable) else 0
        return pool_total, pool_available
    except Exception:
        memory = psutil.virtual_memory()
        fallback = int(memory.available)
        return fallback, fallback


# The CFS period the kernel uses when a Sysresources declares a quota but no period,
# so `cpu_quota / cpu_period` reads as "cores" either way.
_DEFAULT_CPU_PERIOD_US: Final[int] = 100_000

# blkio.weight's accepted range. A declaration outside it is not a capacity this host
# lacks, it is a value the runtime will refuse, so it is worth catching at admission.
_BLKIO_WEIGHT_RANGE: Final[Tuple[int, int]] = (10, 1000)


def _requested_cores(at_most: celaut.Sysresources) -> float:
    """The CPU quota expressed in cores, or 0.0 when none is declared."""
    if not at_most.cpu_quota:
        return 0.0
    return float(at_most.cpu_quota) / float(at_most.cpu_period or _DEFAULT_CPU_PERIOD_US)


def _sysreq_shortfalls(
        at_most: celaut.Sysresources,
        *,
        disk_free: int,
        cpu_total: int,
        pool_total: int,
        pool_available: int,
) -> List[str]:
    """Every declared limit this host cannot honour, as one reason each.

    All of them, not the first: a service asking for more memory *and* more disk than
    exists should be told both, or it fixes one and comes back for the other.

    The three limits are not checked against the same thing, deliberately:

    * **Memory and disk are exclusive.** Bytes handed to one instance are bytes no
      other instance can have, so they are checked against what is *free right now*.
    * **A CPU quota is a share of time.** Every instance on the host already shares
      the same cores, so "free right now" is not the question -- the question is
      whether the machine has that many cores at all. Checking it against
      instantaneous load would make admission flap with every spike, and would refuse
      a service the host can perfectly well run a moment later.
    * **blkio_weight is neither**; it is a relative priority, so the only thing that
      can be wrong with it is being outside the range cgroups accept.
    """
    shortfalls: List[str] = []

    if not could_ve_this_sysreq(at_most):
        shortfalls.append(
            "Insufficient memory for resources.at_most.mem_limit. "
            f"Requested: {int(at_most.mem_limit)} bytes, "
            f"available in service memory pool: {pool_available} bytes, "
            f"total service memory pool: {pool_total} bytes."
            "\n Try `sudo nodo daemon restart` to free up memory or increase the memory pool size if possible."
        )

    if at_most.disk_space and int(at_most.disk_space) > disk_free:
        shortfalls.append(
            "Insufficient disk for resources.at_most.disk_space. "
            f"Requested: {int(at_most.disk_space)} bytes, free on the filesystem: {disk_free} bytes."
        )

    requested_cores = _requested_cores(at_most)
    # cpu_total is 0 when psutil cannot count physical cores; an unknown capacity is
    # not evidence of an insufficient one, so the check is skipped rather than failed.
    if requested_cores and cpu_total and requested_cores > cpu_total:
        shortfalls.append(
            "Insufficient CPU for resources.at_most.cpu_quota/cpu_period. "
            f"Requested: {requested_cores:.2f} cores "
            f"(quota {int(at_most.cpu_quota)}us / period {int(at_most.cpu_period) or _DEFAULT_CPU_PERIOD_US}us), "
            f"this host has {cpu_total}."
        )

    low, high = _BLKIO_WEIGHT_RANGE
    if at_most.blkio_weight and not (low <= int(at_most.blkio_weight) <= high):
        shortfalls.append(
            f"resources.at_most.blkio_weight is {int(at_most.blkio_weight)}, outside the "
            f"accepted range {low}-{high}."
        )

    return shortfalls


def get_resource_availability(resources: celaut.Service.Container.Resources) -> Dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_total = psutil.cpu_count(logical=False) or 0
    # Non-blocking: `interval=None` is usage since the previous call. A blocking
    # sample costs 100 ms on every launch and, since this function is also the body of
    # the GetResourceAvailability RPC, lets a peer make this host pay it (#288).
    # Nothing reads `system_cpu_available_percent` -- the CPU check above is against
    # total capacity, not instantaneous load, on purpose -- but the field is part of a
    # payload that travels to peers, so it is kept: what it costs to sample is a
    # separate question from what removing a wire-visible field would break.
    cpu_available_percent = max(0.0, 100.0 - psutil.cpu_percent(interval=None))

    service_memory_pool_total, service_memory_pool_available = _get_service_memory_snapshot()

    requested_mem_limit = 0
    if resources and resources.HasField("at_most") and resources.at_most.HasField("mem_limit"):
        requested_mem_limit = int(resources.at_most.mem_limit)

    shortfalls: List[str] = []
    if resources and resources.HasField("at_most"):
        shortfalls = _sysreq_shortfalls(
            resources.at_most,
            disk_free=int(disk.free),
            cpu_total=int(cpu_total),
            pool_total=service_memory_pool_total,
            pool_available=service_memory_pool_available,
        )

    can_execute = not shortfalls
    reason = " | ".join(shortfalls)

    return {
        "can_execute": can_execute,
        "reason": reason,
        "requested_mem_limit": requested_mem_limit,
        "service_memory_pool_total": service_memory_pool_total,
        "service_memory_pool_available": service_memory_pool_available,
        "system_memory_total": int(memory.total),
        "system_memory_available": int(memory.available),
        "system_disk_total": int(disk.total),
        "system_disk_free": int(disk.free),
        "system_cpu_total": int(cpu_total),
        "system_cpu_available_percent": float(cpu_available_percent),
    }
