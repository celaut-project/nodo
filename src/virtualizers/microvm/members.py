"""Who is in the family.

Two hypervisors, one table, and nothing else about them: a backend's launch and
its hotplug are its own (see ``ch/execute.py``, ``qemu/execute.py``), reached
through ``src.virtualizers.registry``. What is here is only what the family's
shared code -- teardown, health check, janitor -- needs in order to act on a
guest without asking which backend booted it.

This table is deliberately the *only* place the family names its members, and it
must stay a closed, tiny list. A third Linux microVM hypervisor (Firecracker) is
five lines here plus its own ``execute``. Anything that is not a locally booted
Linux microVM does not belong in it: it registers as its own family in
``src.virtualizers.registry`` and never appears below. See ``docs/BACKENDS.md``.
"""
from typing import Dict, Optional

from src.virtualizers.microvm.hypervisor import Hypervisor

FAMILY = "microvm"

CH = Hypervisor(
    name="ch",
    log_tag="CH",
    process_prefix="nodo-ch-",
    socket_prefix="ch-",
)

QEMU = Hypervisor(
    name="qemu",
    log_tag="QEMU",
    process_prefix="nodo-qemu-",
    socket_prefix="qmp-",
)

MEMBERS: Dict[str, Hypervisor] = {CH.name: CH, QEMU.name: QEMU}


def member(name: str) -> Optional[Hypervisor]:
    """The hypervisor a runtime-state entry names, or ``None`` if it names none.

    ``None`` rather than a default on purpose. Defaulting an unrecognized name to
    CH is what had the janitor judge a QEMU guest with CH's process matcher and
    reap it while healthy (#295); an entry nobody in this family claims is a fact
    to report, not one to guess at.
    """
    return MEMBERS.get(str(name or "").strip().lower())
