"""Per-architecture resources a node announces (`Peer.resources`) and the peer pre-filter.

Two halves of one contract (#459, absorbing #454):

* **What this node announces**: one ``ArchitectureResources`` per architecture it can
  boot, whose ``resources`` is the most one instance of that architecture could
  be granted here -- cores, memory and disk as the machine has them, capped by the
  operator's ``host_limits`` -- and this node's measured per-core ``benchmark`` scores for
  that architecture. Ceilings, not headroom: they describe the machine, so the signed
  announcement only changes when the machine, its caps or its scores do, and the
  announcement cache (``gateway.utils._sign_peer``) keeps hitting.

* **What a node does with a peer's**: before asking a peer ``GetServiceEstimatedCost``
  or ``GetResourceAvailability``, check the last ``Peer`` it announced (kept verbatim in
  ``peer.advertisement``). A peer that announced resources but not the service's
  architecture does not run it; one whose announced ceilings or scores the request does
  not fit could never admit it. Either way the round-trip is skipped. A peer that
  announced nothing is asked as before, and so is any request whose architecture this
  node cannot name.

The pre-filter only ever skips what can never fit. A request within the ceilings can
still be refused for load, and that is what the call is for.
"""
from typing import Iterable, List, Optional, Sequence, Tuple

from protos import celaut_pb2 as celaut
from src.utils import benchmark, host_limits, keyvalue, logger as log
from src.utils.arch_guard import arch_from_tags

CPU_PERIOD_US = host_limits.DEFAULT_CPU_PERIOD_US


def _cores_bytes_disk() -> Tuple[Optional[float], Optional[int], Optional[int]]:
    """(cores, memory bytes, disk bytes) one instance could be granted, each None if unknown."""
    cores, ram, disk = host_limits.host_totals()
    caps = host_limits.ceilings()
    if caps is not None:
        if caps.cores is not None:
            cores = min(cores, caps.cores) if cores else caps.cores
        if caps.ram_bytes is not None:
            ram = min(ram, caps.ram_bytes) if ram else caps.ram_bytes
        if caps.disk_bytes is not None:
            disk = min(disk, caps.disk_bytes) if disk else caps.disk_bytes
    return cores, ram, disk


def served_architectures() -> List[List[str]]:
    """Alias lists (canonical first) of every architecture this node can boot."""
    from src.utils.architectures import SUPPORTED_ARCHITECTURES

    return [list(aliases) for aliases in SUPPORTED_ARCHITECTURES if aliases]


def announced_resources(served: Optional[Iterable[Sequence[str]]] = None) -> List[celaut.ArchitectureResources]:
    """This node's ``Peer.resources``: one entry per architecture it serves.

    ``resources`` carries every ceiling that is known -- an unknown one is left unset,
    which a reader takes as "not stated", never as 0 -- and only the benchmark scores
    actually measured: a ``-1`` is absent, so a peer leaves that primitive to the call.
    """
    cores, ram, disk = _cores_bytes_disk()
    entries = []
    for aliases in served_architectures() if served is None else served:
        entry = celaut.ArchitectureResources()
        entry.architecture.tags.extend(aliases)
        at_most = entry.resources
        if cores:
            at_most.cpu_period = CPU_PERIOD_US
            at_most.cpu_quota = int(cores * CPU_PERIOD_US)
        if ram:
            at_most.mem_limit = int(ram)
        if disk:
            at_most.disk_space = int(disk)
        keyvalue.from_dict(at_most.benchmark, benchmark.measured(benchmark.node_scores(aliases[0])))
        entries.append(entry)
    return entries


def ask_of(resources: celaut.Service.Container.Resources) -> celaut.Sysresources:
    """The shape a service's ``Container.Resources`` asks a node about: one ``Sysresources``.

    The limits are ``at_most`` -- what one instance may grow to, the worst case, so a node
    that supports it supports any smaller start -- and ``benchmark`` is ``at_init``'s,
    the minimum per-core score required. ``at_most.benchmark`` carries no meaning and is
    not carried over. This is what ``ArchitectureResources.resources`` holds in a
    ``GetResourceAvailability`` request.
    """
    ask = celaut.Sysresources()
    if resources.HasField("at_most"):
        ask.CopyFrom(resources.at_most)
        ask.ClearField("benchmark")
    if resources.HasField("at_init"):
        ask.benchmark.extend(resources.at_init.benchmark)
    return ask


def container_resources_of(ask: celaut.Sysresources) -> celaut.Service.Container.Resources:
    """Inverse of :func:`ask_of`: the ``Container.Resources`` admission is written against."""
    resources = celaut.Service.Container.Resources()
    resources.at_most.CopyFrom(ask)
    resources.at_most.ClearField("benchmark")
    if len(ask.benchmark):
        resources.at_init.benchmark.extend(ask.benchmark)
    return resources


def request_misfit(
        announced: Sequence[celaut.ArchitectureResources],
        arch: Optional[str],
        ask: celaut.Sysresources,
) -> Optional[str]:
    """Why a peer that announced ``announced`` could never admit this request, or None.

    None -- "ask it" -- when the peer announced nothing (a node that says nothing is
    called as before) or when the request's architecture is unknown here. Otherwise the
    first reason the request cannot fit: the architecture is not announced at all, or a
    declared limit exceeds the announced ceiling, or a required benchmark is above the
    announced measured score (under the same working-set rule admission applies). A
    ceiling or score the peer did not announce is no reason to skip it.
    """
    if not announced or not arch:
        return None
    entry = next(
        (e for e in announced if arch_from_tags(e.architecture.tags) == arch), None
    )
    if entry is None:
        tags = sorted({arch_from_tags(e.architecture.tags) or "?" for e in announced})
        return f"it does not run {arch} (it announced {', '.join(tags)})"

    cap = entry.resources
    reasons = []
    for field in ("mem_limit", "disk_space"):
        if cap.HasField(field) and ask.HasField(field) and getattr(ask, field) > getattr(cap, field):
            reasons.append(
                f"{field} {getattr(ask, field)} is above its announced {getattr(cap, field)}"
            )
    asked_cores = host_limits.requested_cores(ask.cpu_quota, ask.cpu_period)
    if cap.HasField("cpu_quota") and asked_cores:
        cap_cores = host_limits.requested_cores(cap.cpu_quota, cap.cpu_period)
        if asked_cores > cap_cores:
            reasons.append(f"{asked_cores:.2f} cores is above its announced {cap_cores:.2f}")
    if len(ask.benchmark):
        found, _ = benchmark.shortfalls(
            keyvalue.to_dict(ask.benchmark),
            keyvalue.to_dict(cap.benchmark),
            where=f"by the peer for {arch}",
        )
        reasons.extend(found)
    return "; ".join(reasons) or None


def stored_announcement(peer_id: str) -> List[celaut.ArchitectureResources]:
    """The ``resources`` of the last ``Peer`` stored for ``peer_id``; [] if none or unreadable.

    An announcement stored before ``Peer.resources`` existed reads as having none, so
    that peer is called as before.
    """
    try:
        from src.database.sql_connection import SQLConnection

        blob = SQLConnection().get_peer_advertisement(peer_id)
        if not blob:
            return []
        peer = celaut.Peer()
        peer.ParseFromString(blob)
        return list(peer.resources)
    except Exception:
        return []


def should_skip_peer(
        peer_id: str,
        arch: Optional[str],
        ask: celaut.Sysresources,
        rpc: str,
) -> bool:
    """True when ``peer_id``'s own announcement says the ``rpc`` call is pointless. Logs why."""
    reason = request_misfit(stored_announcement(peer_id), arch, ask)
    if reason:
        log.LOGGER(f"Skipping {rpc} on peer {peer_id}: by its own announcement, {reason}.")
        return True
    return False
