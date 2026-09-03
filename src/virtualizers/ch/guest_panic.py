"""Detecting that a guest kernel has died inside a hypervisor that has not.

A panicking Linux kernel does not exit. With no ``panic=`` timeout on its
cmdline it never asks for a reset, so it spins in place forever -- and under TCG
that spin is real work on a host core. The hypervisor process stays alive, its
control socket keeps answering, and its vCPU thread keeps burning CPU, so every
liveness test that asks about the *process* reports a healthy VM. One core per
dead guest, held until an operator notices.

Only the guest's own console says otherwise, which is why this reads the serial
log the launchers already write (``SERIAL_MODE: file``) rather than asking the
hypervisor. A panic is also the last thing a kernel prints, so the marker is
always within the tail -- there is no need to read a log of unbounded size.

A service writing to its own console could forge the marker and have the node
shut it down. That is a service killing itself with extra steps: the blast
radius is its own instance, and no reachable state of another instance depends
on it. Requiring the marker at the start of a line, optionally behind a kernel
printk timestamp, keeps ordinary output from tripping it by accident.
"""
import os
import re
from typing import Any, Dict, Optional

# `Kernel panic - not syncing: <reason>` is printed by panic() itself, for every
# arch and every reason, and is the one line guaranteed to appear. The trailing
# `---[ end Kernel panic ...` banner is not: a panic that dies earlier never
# reaches it.
_PANIC_RE = re.compile(
    r"^(?:\[\s*\d+\.\d+\]\s*)?(Kernel panic - not syncing:.*)$",
    re.MULTILINE,
)

# Generous enough to hold a panic banner plus the backtrace that follows it,
# small enough that the check costs one short read per VM per tick.
SERIAL_TAIL_BYTES = 64 * 1024


def read_serial_tail(path: str, size: int = SERIAL_TAIL_BYTES) -> str:
    """The last ``size`` bytes of a serial log, or "" if it cannot be read."""
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(-size, os.SEEK_END)
            except OSError:
                # Shorter than the window: read what there is.
                fh.seek(0)
            return fh.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return ""
    except Exception:
        return ""


def guest_panic_line(state: Optional[Dict[str, Any]]) -> Optional[str]:
    """The guest's panic message, or ``None`` if it has not panicked.

    ``None`` also covers "cannot tell": a VM with no serial log recorded, or one
    whose log is unreadable, is left alone. Reaping on absent evidence would kill
    healthy guests whenever a log went missing.
    """
    path = str((state or {}).get("serial_log") or "").strip()
    if not path:
        return None
    match = _PANIC_RE.search(read_serial_tail(path))
    return match.group(1).strip() if match else None
