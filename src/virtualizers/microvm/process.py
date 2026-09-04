"""The identity of a microVM's hypervisor process, and whether it is still alive.

A PID alone does not identify a VM. PIDs are recycled, and a node that trusts a
recorded one will eventually signal, or report as healthy, whatever unrelated
process inherited it. So each launch renames its hypervisor process to
``<prefix><id8>`` (``argv[0]``, via ``Popen(args, executable=...)``) and the node
matches that name before believing anything about the PID.

The prefix differs per hypervisor (``nodo-ch-`` / ``nodo-qemu-``) and the name is
recorded in the VM's runtime state at launch, so every later reader -- the health
check, the teardown, the janitor, ``nodo instances`` -- matches against what the
launcher actually named, and none of them has to know which backend wrote it.
Asking one backend's matcher about another backend's guest is what reaped a
healthy VM in #295.
"""
import os
import secrets

from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_bytes

env_manager = ConfigManager()
HASH_SPEC = get_configured_hash_spec(env_manager)


def generate_vmachine_id() -> str:
    """A fresh VM id: random bytes under the node's configured hash spec.

    Shaped like every other id the node handles so the same tooling reads it.
    """
    return hash_bytes(secrets.token_bytes(32), HASH_SPEC).hex()


def visible_process_name(process_prefix: str, vmachine_id: str) -> str:
    """What the hypervisor process for ``vmachine_id`` calls itself in ``/proc``.

    Truncated to 8 hex characters: enough to be unique among the VMs one node
    runs, and short enough to stay legible in ``ps`` and in the logs.
    """
    return f"{process_prefix}{vmachine_id[:8]}"


def proc_state(pid: int) -> str:
    """The single-letter state from ``/proc/<pid>/stat``.

    ``""`` when the process is gone, ``"?"`` when it cannot be read. Only ``Z``
    is acted on -- a zombie is a process the node can still signal and still
    find in ``/proc``, while the VM inside it is long gone.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            stat = f.read()
    except FileNotFoundError:
        return ""
    except PermissionError:
        return "?"
    except Exception:
        return "?"

    # The process name sits in parentheses with the state right after it. Split
    # from the right, because a process name may itself contain spaces.
    try:
        return stat.rsplit(")", 1)[1].strip().split()[0]
    except Exception:
        return "?"


def pid_matches(pid: int, process_name: str) -> bool:
    """Is ``pid`` the process a launch named ``process_name``?

    Unreadable ``/proc`` entries answer yes: a process the node cannot inspect
    still exists, and treating it as absent would have the janitor delete the
    state of a VM it then cannot kill.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw_cmdline = f.read()
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True

    if not raw_cmdline:
        return False
    argv = [arg.decode("utf-8", errors="replace") for arg in raw_cmdline.split(b"\0") if arg]
    return bool(argv) and os.path.basename(argv[0]) == process_name


def pid_alive(pid: int, process_name: str = "") -> bool:
    """Is this PID still the live hypervisor of the VM that recorded it?

    ``process_name`` empty skips the identity check, which is only right for
    callers that have already established what the PID is.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # If we cannot signal it, the process still exists.
        return True
    except Exception:
        return False

    if proc_state(pid) == "Z":
        return False
    if process_name and not pid_matches(pid=pid, process_name=process_name):
        return False
    return True
