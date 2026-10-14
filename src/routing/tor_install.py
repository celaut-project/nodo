"""On-demand install of nodo's own copy of Tor.

Tor is not installed with the node. ``bash/install_tor.sh`` provisions it under
``dependencies.tor.RUNTIME_ROOT`` (``runtime/tor`` by default) the first time the
operator turns it on, the same way ``install_buildkit.sh`` and ``install_java.sh``
provision theirs. Nothing is installed system-wide and ``/etc/tor`` is never read.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from src.utils.config import ConfigManager

env_manager = ConfigManager()


class TorInstallError(RuntimeError):
    """Tor is needed but could not be provisioned."""


def main_dir() -> str:
    value = env_manager.get("main.MAIN_DIR")
    return str(value) if value else os.getcwd()


def runtime_root() -> Path:
    configured = env_manager.get("dependencies.tor.RUNTIME_ROOT")
    return Path(str(configured)) if configured else Path(main_dir()) / "runtime" / "tor"


def managed_binary() -> Path:
    """Where the installer leaves the ``tor`` executable (its libraries sit beside it)."""
    return runtime_root() / "current" / "tor" / "tor"


def install_command() -> str:
    return f"/bin/bash {main_dir()}/bash/install_tor.sh {main_dir()}"


def tor_installed(binary: Optional[Path] = None) -> bool:
    path = binary or managed_binary()
    return path.is_file() and os.access(path, os.X_OK)


def ensure_tor_installed() -> None:
    """Run the installer unless the managed ``tor`` is already there."""
    if tor_installed():
        return
    command = install_command()
    print(f"nodo's own Tor is not installed. Installing it now:\n  {command}")
    result = subprocess.run(command, shell=True)
    if result.returncode != 0 or not tor_installed():
        raise TorInstallError(
            f"Could not install Tor. Install it by hand with `{command}`."
        )
