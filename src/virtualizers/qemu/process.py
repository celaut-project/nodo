"""Process identity checks for QEMU guests.

Mirrors :mod:`src.virtualizers.ch.process` but matches the QEMU launcher's
visible process name (``nodo-qemu-<id8>``) instead of CH's, so a recycled PID
belonging to some unrelated process is never mistaken for a live guest.
"""
import os


QEMU_PROCESS_PREFIX = "nodo-qemu-"


def proc_state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            stat = f.read()
    except FileNotFoundError:
        return ""
    except PermissionError:
        return "?"
    except Exception:
        return "?"

    try:
        return stat.rsplit(")", 1)[1].strip().split()[0]
    except Exception:
        return "?"


def qemu_process_name(vmachine_id: str) -> str:
    return f"{QEMU_PROCESS_PREFIX}{vmachine_id[:8]}"


def pid_matches_vmachine(pid: int, vmachine_id: str) -> bool:
    expected_name = qemu_process_name(vmachine_id)
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
    return bool(argv) and os.path.basename(argv[0]) == expected_name


def pid_alive(pid: int, vmachine_id: str = "") -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False

    if proc_state(pid) == "Z":
        return False
    if vmachine_id and not pid_matches_vmachine(pid=pid, vmachine_id=vmachine_id):
        return False
    return True
