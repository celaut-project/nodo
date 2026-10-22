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

import errno
import os
import re
import shlex
import shutil
import sys
from typing import List, Optional, Sequence, Tuple

CAP_NET_ADMIN = 12
CAP_NET_RAW = 13
CAP_SYS_ADMIN = 21

_STATUS_FILE = "/proc/self/status"


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


# Who the daemon runs as when it is not root. Root's file, written by the
# installers (bash/lib_privileged.sh). A command that runs as root reads it from
# here and never from config.yaml: that user can write config.yaml.
SERVICE_USER_RECORD = "/etc/nodo/service-user"
NODO_UNIT_FILE = "/etc/systemd/system/nodo.service"
_USER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,31}")
_UNIT_USER = re.compile(r"^\s*User\s*=\s*(\S+)\s*$", re.MULTILINE)


def valid_user_name(name: str) -> bool:
    return bool(_USER_NAME.fullmatch(name or ""))


def read_service_user_record(path: str = SERVICE_USER_RECORD) -> str:
    """The user in the record, or ``""`` when there is none.

    Raises ``ValueError`` when the record, or its directory, can be written by
    anyone but root, or when it does not hold one user name.
    """
    if not os.path.lexists(path):
        return ""
    for item in (path, os.path.dirname(path)):
        info = os.stat(item)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"{item} must be owned by root and writable only by root.")
    with open(path, "r", encoding="utf-8") as handle:
        name = handle.read().rstrip("\n")
    if not valid_user_name(name):
        raise ValueError(f"{path} does not hold a valid user name: {name!r}.")
    return name


def unit_user(unit_content: str) -> str:
    """The last ``User=`` of a unit (the one systemd uses), or ``""``."""
    users = _UNIT_USER.findall(unit_content or "")
    return users[-1] if users else ""


def installed_service_user(
    record: str = SERVICE_USER_RECORD, unit_file: str = NODO_UNIT_FILE
) -> str:
    """The user the daemon runs as, from root's sources only; ``""`` for root.

    The record first, else the ``User=`` of the installed unit (a node set up
    before the record existed). Same rule as ``installed_service_user`` in
    ``bash/lib_privileged.sh``.
    """
    name = read_service_user_record(record)
    if not name and os.path.isfile(unit_file):
        with open(unit_file, "r", encoding="utf-8") as handle:
            name = unit_user(handle.read())
    if name == "root":
        return ""
    if name and not valid_user_name(name):
        raise ValueError(f"The User= of {unit_file} is not a valid user name: {name!r}.")
    return name


# The directories sudo searches by default (secure_path). An install without
# sudo puts its wrapper in ~/.local/bin, which is not one of them.
SUDO_SECURE_PATH = (
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin", "/snap/bin",
)


def sudo_nodo(arguments: str = "") -> str:
    """The command that runs ``nodo <arguments>`` as root, as an operator types it.

    ``sudo nodo`` when sudo finds the wrapper on its own path. When the wrapper
    is somewhere sudo does not search (``~/.local/bin`` after an install without
    sudo), its absolute path, so the command the message prints is the one that
    works.
    """
    wrapper = shutil.which("nodo")
    if wrapper is None:
        candidate = os.path.join(os.path.expanduser("~"), ".local", "bin", "nodo")
        wrapper = candidate if os.access(candidate, os.X_OK) else None
    command = "nodo"
    if wrapper is not None and os.path.dirname(wrapper) not in SUDO_SECURE_PATH:
        command = shlex.quote(wrapper)
    return f"sudo {command} {arguments}".rstrip()


def sudo_rerun() -> str:
    """``sudo nodo`` followed by the arguments this process was started with."""
    return sudo_nodo(" ".join(shlex.quote(a) for a in sys.argv[1:]))


# setpriv (util-linux): clears the inheritable and ambient sets, then execs.
SETPRIV_DROP_ALL = ("--inh-caps=-all", "--ambient-caps=-all", "--")


def without_capabilities(
    argv: Sequence[str], executable: Optional[str] = None
) -> Tuple[List[str], Optional[str]]:
    """``(args, executable)`` for ``Popen`` that start a program with no capabilities.

    A daemon that systemd starts with ``AmbientCapabilities=CAP_NET_ADMIN`` passes
    that capability to every program it execs. cloud-hypervisor, QEMU and
    virtiofsd do not need it: the daemon creates each tap owned by its own user
    and brings it UP first, so the hypervisor only opens it. ``setpriv`` clears the
    inheritable and ambient sets before it execs the program, which then starts
    with empty permitted and effective sets (it has no file capabilities). Not a
    ``preexec_fn``: the daemon runs gRPC threads, and Python does not support one
    in a process with threads (the child can deadlock before it execs).

    ``argv[0]`` is kept when it differs from ``executable``: the node names each
    hypervisor process by it (``src/virtualizers/microvm/process.py``), and
    setpriv execs its first argument as given, so bash's ``exec -a`` sets it.

    Unchanged under root: an exec by uid 0 gets every capability whatever the
    ambient set says, so a root install runs its children exactly as before.
    Raises ``OSError`` when setpriv is missing, rather than start the program
    with the daemon's capability.
    """
    args = list(argv)
    if os.geteuid() == 0:
        return args, executable
    setpriv = shutil.which("setpriv")
    if not setpriv:
        raise OSError(
            errno.ENOENT,
            "setpriv (util-linux) is not installed; the node will not start "
            f"{executable or args[0]} with its own CAP_NET_ADMIN. Install util-linux.",
        )
    if executable is None or executable == args[0]:
        return [setpriv, *SETPRIV_DROP_ALL, *args], None
    return [
        setpriv, *SETPRIV_DROP_ALL,
        "/bin/bash", "-c", 'exec -a "$0" "$@"', args[0], executable, *args[1:],
    ], None
