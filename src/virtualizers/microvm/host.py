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
