"""What this process may do, asked of the kernel instead of inferred from its uid.

``os.geteuid() == 0`` answers the wrong question. The firewall, the guest bridge,
the taps and the ``net.*`` sysctls need ``CAP_NET_ADMIN``, not uid 0: a root
process in a container without that capability cannot do them, and a service
user that systemd gives ``AmbientCapabilities=CAP_NET_ADMIN`` can. A guard that
checks only the uid refuses that service user, so every network guard accepts the
capability as well.

Reads ``CapEff`` from ``/proc/self/status``, so nothing here needs libcap. Uid 0
still passes every check without reading it, so a root install takes exactly the
paths it took before.

Kept free of ``src.utils.config`` and ``src.utils.logger``: the firewall package
imports this module, and ``ConfigManager`` imports the firewall package.
"""

import ctypes
import os
import sys
from typing import Optional

CAP_NET_ADMIN = 12
CAP_NET_RAW = 13
CAP_SYS_ADMIN = 21

_STATUS_FILE = "/proc/self/status"

# prctl(2): PR_CAP_AMBIENT, and its PR_CAP_AMBIENT_CLEAR_ALL operation.
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4


def effective_capabilities(status_file: str = _STATUS_FILE) -> Optional[int]:
    """The ``CapEff`` bit mask of this process, or ``None`` when it cannot be read."""
    try:
        with open(status_file, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("CapEff:"):
                    return int(line.split(":", 1)[1].strip(), 16)
    except (OSError, ValueError):
        return None
    return None


def has_capability(capability: int, status_file: str = _STATUS_FILE) -> bool:
    """True when this process is root or ``capability`` is in its effective set.

    Root passes as it always has, so a root install behaves exactly as before; the
    capability is what lets a service user through as well.
    """
    if not hasattr(os, "geteuid"):
        return False
    if os.geteuid() == 0:
        return True
    mask = effective_capabilities(status_file)
    return bool(mask is not None and mask & (1 << capability))


def can_admin_network() -> bool:
    """Taps, the bridge, nftables/iptables and ``net.*`` sysctls: ``CAP_NET_ADMIN``."""
    return has_capability(CAP_NET_ADMIN)


def can_create_network_namespace() -> bool:
    """``ip netns add`` needs ``CAP_SYS_ADMIN`` on top of ``CAP_NET_ADMIN``."""
    return has_capability(CAP_SYS_ADMIN) and has_capability(CAP_NET_ADMIN)


def _load_prctl():
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None


# Resolved here, in the parent, so the child does not have to dlopen anything
# between fork and exec.
_PRCTL = _load_prctl()


def exec_without_capabilities() -> None:
    """``preexec_fn`` that makes the exec'd program start with no capabilities.

    A daemon that systemd starts with ``AmbientCapabilities=CAP_NET_ADMIN`` passes
    that capability to every program it execs. cloud-hypervisor, QEMU and
    virtiofsd do not need it: the daemon creates each tap owned by its own user
    and brings it UP first, so the hypervisor only opens it. Clearing the ambient
    set before exec leaves a non-root child with empty permitted and effective
    sets (a program without file capabilities gets only the ambient ones).

    A no-op under root: an exec by uid 0 gets every capability whatever the
    ambient set says, so a root install runs its children exactly as before.
    """
    if os.geteuid() == 0 or _PRCTL is None:
        return
    if _PRCTL(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"prctl(PR_CAP_AMBIENT_CLEAR_ALL) failed: {os.strerror(error)}")
