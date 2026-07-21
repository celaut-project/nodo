"""Docker-free helpers describing nodo's *isolated* Docker toolchain.

nodo does not use Docker as a runtime (Cloud Hypervisor is the only virtualizer).
Docker is an **optional** dependency needed solely by the local packer
(`packer.local: true`). To keep the CH-only runtime completely Docker-free, this
module:

  * never imports the `docker` Python library, and
  * never raises at import time — even on nodes that have never installed Docker.

It only computes the paths/environment used to drive nodo's *isolated* Docker
daemon (a dockerd bound to a private socket + data-root under MAIN_DIR, installed
by ``bash/install_docker.sh`` regardless of any host Docker). Presence is checked
lazily via :func:`docker_binaries_installed`; the packer installs Docker on demand
before it ever uses these values.
"""
import os
from pathlib import Path

from src.utils.config import ConfigManager

config = ConfigManager()


def _main_dir() -> Path:
    main_dir = config.get("main.MAIN_DIR")
    if main_dir:
        return Path(str(main_dir)).expanduser().resolve()
    # Fall back to the repository root (this file is src/utils/docker_env.py).
    return Path(__file__).resolve().parents[2]


NODO_ROOT = _main_dir()
DEFAULT_BIN_DIR = NODO_ROOT / "bin"

# Isolated Docker toolchain paths (overridable via dependencies.docker.* in config).
DOCKER_BIN = str(config.get("dependencies.docker.BIN") or (DEFAULT_BIN_DIR / "docker"))
DOCKERD_BIN = str(config.get("dependencies.docker.DAEMON_BIN") or (DEFAULT_BIN_DIR / "dockerd"))
BUILDX_BIN = str(
    config.get("dependencies.docker.BUILDX_BIN")
    or (NODO_ROOT / "libexec" / "docker" / "cli-plugins" / "docker-buildx")
)

BIN_DIR = Path(DOCKER_BIN).resolve().parent
PLUGIN_DIR = Path(BUILDX_BIN).resolve().parent

# The isolated daemon binds this private socket (see bash/start_docker_daemon.sh).
DOCKER_SOCKET = str(
    config.get("dependencies.docker.DOCKER_SOCKET")
    or (NODO_ROOT / "docker" / "docker.sock")
)

# Environment that points any `docker`/`docker buildx` invocation at nodo's
# isolated daemon and CLI plugins — never the host's Docker.
DOCKER_ENV = os.environ.copy()
DOCKER_ENV.update(
    {
        "DOCKER_CLI_PLUGINS_DIR": str(PLUGIN_DIR),
        "DOCKER_API_VERSION": "1.43",
        "DOCKER_HOST": f"unix://{DOCKER_SOCKET}",
        "PATH": f"{BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
        "DOCKER_CONFIG": str(NODO_ROOT / "libexec" / "docker"),
    }
)

DOCKER_COMMAND = [DOCKER_BIN]


def docker_binaries_installed() -> bool:
    """True when nodo's isolated dockerd + docker client are present."""
    return os.path.isfile(DOCKERD_BIN) and os.path.isfile(DOCKER_BIN)
