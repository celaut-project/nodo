"""Reading back what a guest printed.

The hypervisor writes the guest's serial console to a file in the VM's runtime
directory, and it is the only window into a boot that never reached the network.
Both backends tail it for the same two purposes: the tail attached to a failed
launch, and the one class of failure worth aborting a launch early for -- the
initramfs refusing to hand over to the service.
"""
from pathlib import Path
from typing import Optional


def tail_file(path: Path, max_lines: int = 40) -> str:
    """The last ``max_lines`` of a log, or a bracketed reason it could not be read.

    Never raises and never returns nothing: this runs on the failure path of a
    launch, where an unreadable log must not replace the error that is being
    reported.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return "<missing>"
    except Exception as e:
        return f"<unreadable: {e}>"

    if not lines:
        return "<empty>"
    return "".join(lines[-max_lines:]).strip()


def detect_initramfs_fatal(serial_log_path: Optional[Path]) -> Optional[str]:
    """The line saying the guest's initramfs gave up, if it did.

    The nodo initramfs prints a tagged ERROR and then parks in a fatal loop, so
    the process stays alive and the network never comes up. Without this the
    launch spends its whole readiness timeout waiting for a guest that already
    decided not to boot, and reports a timeout instead of the reason.
    """
    if not serial_log_path:
        return None
    serial_tail = tail_file(serial_log_path, max_lines=200)
    if not serial_tail or serial_tail.startswith("<"):
        return None

    fatal_line: Optional[str] = None
    for line in serial_tail.splitlines():
        line_stripped = line.strip()
        if "[nodo-ch-initramfs] ERROR:" in line_stripped:
            fatal_line = line_stripped
            continue
        if "Kernel panic - not syncing: Attempted to kill init!" in line_stripped:
            fatal_line = line_stripped
    return fatal_line
