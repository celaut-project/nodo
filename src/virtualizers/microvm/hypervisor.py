"""What the family needs to know about one of its members.

The whole membership contract, and deliberately nothing more: a name, a log tag,
how the launcher names its process and how its control socket is named. Given
those four facts the family can boot-track, health-check, tear down and sweep a
guest without knowing which hypervisor produced it -- which is what let ``kill``,
``maintain`` and the janitor stop existing twice over.

The members themselves are listed in :mod:`src.virtualizers.microvm.members`, in
the family rather than in the backends: knowing who belongs to it is the family's
own business, and keeping the list here means the family never has to import a
backend to sweep, kill or health-check one of its guests.

Note what is *not* in here: no ``execute``, no ``hotplug``, no base class to
inherit. Launching is where CH and QEMU genuinely differ (an API socket and KVM
against a QMP socket and TCG), and hotplug differs with it (a cgroup move against
a balloon resize). Those stay in the backends, and the neutral layer reaches them
through ``src.virtualizers.registry``.
"""
from dataclasses import dataclass
from pathlib import Path

from src.virtualizers.microvm import paths, process


@dataclass(frozen=True)
class Hypervisor:
    """One member of the microVM family."""

    name: str
    """Registry key, and what a VM's runtime state records under ``virtualizer``."""

    log_tag: str
    """Bracketed prefix on every log line about one of its guests, e.g. ``CH``."""

    process_prefix: str
    """Prefix of the launcher's visible ``/proc`` name, e.g. ``nodo-ch-``."""

    socket_prefix: str
    """Prefix of its control socket's filename, e.g. ``ch-`` or ``qmp-``."""

    def process_name(self, vmachine_id: str) -> str:
        return process.visible_process_name(self.process_prefix, vmachine_id)

    def control_socket(self, vmachine_id: str) -> Path:
        return paths.control_socket_path(self.socket_prefix, vmachine_id)

    def prefix(self, vmachine_id: str) -> str:
        """The bracketed tag every log line about one of its guests carries."""
        return f"[{self.log_tag}][{vmachine_id}]"

    def log(self, vmachine_id: str, message: str) -> str:
        return f"{self.prefix(vmachine_id)} {message}"
