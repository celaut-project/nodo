"""``nodo resources`` -- what this node announces it can run, per architecture.

The same ``Peer.resources`` ``GetPeerInfo`` signs and hands every peer (#459): for each
architecture this node boots, the most one instance could be granted here -- the
machine's cores, memory and disk capped by ``host_limits`` -- and its measured per-core
benchmark scores. Ceilings, not headroom.

Two audiences, the same shape as ``nodo reputation``: a person reads the printed form,
and the TUI reads ``--json``. The JSON carries the announcement as the serialized
``Peer`` (only ``resources`` set), base64-encoded, so the TUI decodes this node's
announcement with exactly the code it decodes every peer's stored one with (#455) --
one reading of the format, not two.

Worked out by this process from the same config and machine the daemon reads, so it is
what the daemon announces. Read-only.
"""

import base64
import json
import sys
import time
from typing import List, Optional


def _cores(entry) -> Optional[float]:
    """``cpu_quota / cpu_period``, with the kernel's default period when none is given
    (Sysresources.cpu_period). None when no quota is given."""
    from src.utils.cost_functions.resource_availability import _DEFAULT_CPU_PERIOD_US

    at_most = entry.resources
    if at_most.cpu_quota:
        return at_most.cpu_quota / (at_most.cpu_period or _DEFAULT_CPU_PERIOD_US)
    return None


def _print_report(entries) -> None:
    from src.commands.services import format_bytes
    from src.utils.arch_guard import arch_from_tags

    if not entries:
        from src.utils.cost_functions.architecture_resources import executes_locally

        if not executes_locally():
            print("This node announces no resources: it delegates only (network.EXECUTE_LOCALLY: false).")
        else:
            print("This node announces no resources.")
        return
    print("What this node announces to its peers (ceilings, not free capacity):")
    for entry in entries:
        at_most = entry.resources
        arch = arch_from_tags(entry.architecture.tags) or ", ".join(entry.architecture.tags)
        cores = _cores(entry)
        print(f"  {arch}")
        print(f"    cores   {cores:g}" if cores is not None else "    cores   not stated")
        print(f"    memory  {format_bytes(at_most.mem_limit)}" if at_most.HasField("mem_limit") else "    memory  not stated")
        print(f"    disk    {format_bytes(at_most.disk_space)}" if at_most.HasField("disk_space") else "    disk    not stated")
        if not len(at_most.benchmark):
            print("    no benchmark scores measured")
        for score in at_most.benchmark:
            print(f"    {score.key:<36} {score.value} per core")


def resources(argv: Optional[List[str]] = None) -> bool:
    """Print what this node announces it can run. ``nodo resources [--json]``."""
    argv = list(argv or [])
    as_json = "--json" in argv

    try:
        from protos import celaut_pb2 as celaut
        from src.utils.cost_functions.architecture_resources import announced_resources

        entries = announced_resources()
        peer = celaut.Peer()
        peer.resources.extend(entries)
    except Exception as e:
        if as_json:
            # Shaped like a report, so a reader never branches on which object it got.
            json.dump({"error": str(e), "read_at": int(time.time())}, sys.stdout)
            print()
        else:
            print(f"Could not work out the resources this node announces: {e}")
        return _flushed(False)

    if as_json:
        json.dump({
            "peer": base64.b64encode(peer.SerializeToString()).decode("ascii"),
            "read_at": int(time.time()),
        }, sys.stdout)
        print()
    else:
        _print_report(entries)
    return _flushed(True)


def _flushed(result: bool) -> bool:
    """Return ``result`` with stdout on the wire: ``nodo.py`` exits via ``os._exit``."""
    sys.stdout.flush()
    return result
