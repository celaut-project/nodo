"""Running host commands on behalf of a microVM.

Both backends drive the same host tools -- ``ip``, ``sysctl``, ``debugfs``,
``ping`` -- and both need the same thing from a failure: the command line, the
exit code and whatever the tool wrote, in the exception, because a launch that
died inside ``ip link add`` is otherwise reported as an empty
``CalledProcessError``.
"""
import shutil
import subprocess
from typing import List

from src.virtualizers.microvm.errors import MicroVMError


def run(command: List[str], *, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=check,
            capture_output=capture_output,
            text=True,
        )
    except FileNotFoundError as e:
        raise MicroVMError(f"Required command not found: {command[0]}") from e
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        stdout = e.stdout.strip() if e.stdout else ""
        details: List[str] = []
        if stdout:
            details.append(f"stdout={stdout}")
        if stderr:
            details.append(f"stderr={stderr}")
        raise MicroVMError(
            f"Command failed ({e.returncode}): {' '.join(command)} -> "
            f"{' | '.join(details) if details else 'unknown error'}"
        ) from e


def ensure_command_available(command: str) -> None:
    if not shutil.which(command):
        raise MicroVMError(f"Required command not found in PATH: {command}")


def write_sysctl(key: str, value: str) -> None:
    """Set a sysctl and prove it took, raising ``MicroVMError`` when it did not.

    procps ``sysctl -w`` prints "permission denied on key ..., ignoring" and exits
    0 when the kernel refuses the write, so its exit code says nothing about the
    value. Under root that never happens; under a service user without
    ``CAP_NET_ADMIN`` it is the normal outcome, and a caller that trusted the exit
    code would carry on with the setting still off. Reading the key back is the
    only answer that holds.
    """
    run(["sysctl", "-w", f"{key}={value}"])
    current = run(["sysctl", "-n", key]).stdout.strip()
    if current != str(value):
        raise MicroVMError(
            f"sysctl {key} is {current or '<empty>'} after writing {value}: the kernel "
            "refused the write. This needs root or CAP_NET_ADMIN."
        )

