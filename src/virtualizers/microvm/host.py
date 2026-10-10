"""Running host commands on behalf of a microVM.

Both backends drive the same host tools -- ``ip``, ``sysctl``, ``debugfs``,
``ping`` -- and both need the same thing from a failure: the command line, the
exit code and whatever the tool wrote, in the exception, because a launch that
died inside ``ip link add`` is otherwise reported as an empty
``CalledProcessError``.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import List, Union

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


def ensure_private_dir(path: Union[str, os.PathLike]) -> Path:
    """Create ``path`` for this process alone, or refuse a directory someone else owns.

    The control sockets sit in a short flat directory that, by default, used to be
    ``/tmp/nodo-ch``: a name any local user can create first. Whoever owns that
    directory can replace a socket and talk to a hypervisor, or a virtiofsd, in the
    node's place. So the directory must belong to the user the daemon runs as, and
    nobody else may write in it. One this process owns is tightened if it is
    group- or world-writable; one another user owns is refused.
    """
    directory = Path(path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise MicroVMError(f"{directory} is not a directory; refusing to put control sockets in it.")
    if info.st_uid != os.geteuid():
        raise MicroVMError(
            f"{directory} is owned by uid {info.st_uid}, not by this process (uid "
            f"{os.geteuid()}); refusing to put control sockets in it. Remove it, or set "
            "virtualizers.ch.API_SOCKET_DIR to a directory this user owns."
        )
    if stat.S_IMODE(info.st_mode) & 0o022:
        os.chmod(directory, stat.S_IMODE(info.st_mode) & ~0o022)
    return directory
