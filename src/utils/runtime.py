import os
import subprocess
from pathlib import Path

import docker as docker_lib
from protos import celaut_pb2

from src.utils.config import ConfigManager

config = ConfigManager()

# Supported architectures derived from config.
PACKER_SUPPORTED_ARCHITECTURES = []
if config.get("packer.ARM_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(["linux/arm64", "arm64", "arm_64", "aarch64"])
if config.get("packer.X86_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(["linux/amd64", "x86_64", "amd64"])

SUPPORTED_ARCHITECTURES = []
if config.get("builder.ARM_SUPPORT"):
    SUPPORTED_ARCHITECTURES.append(["linux/arm64", "arm64", "arm_64", "aarch64"])
if config.get("builder.X86_SUPPORT"):
    SUPPORTED_ARCHITECTURES.append(["linux/amd64", "x86_64", "amd64"])

# Docker runtime values.
_main_dir = config.get("main.MAIN_DIR")
NODO_ROOT = Path(_main_dir).expanduser().resolve() if _main_dir else Path(__file__).resolve().parents[2]
BIN_DIR = NODO_ROOT / "bin"
PLUGIN_DIR = NODO_ROOT / "libexec" / "docker" / "cli-plugins"

DOCKER_BIN = str(BIN_DIR / "docker")
DOCKERD_BIN = str(BIN_DIR / "dockerd")
DOCKER_SOCKET = config.get("virtualizers.docker.DOCKER_SOCKET") or str(NODO_ROOT / "docker" / "docker.sock")

if not os.path.isfile(DOCKER_BIN):
    raise RuntimeError(f"Cliente Docker de Nodo no encontrado en {DOCKER_BIN}. Ejecuta el instalador.")
if not os.path.isfile(str(PLUGIN_DIR / "docker-buildx")):
    raise RuntimeError(f"Plugin buildx no encontrado en {PLUGIN_DIR}. Ejecuta el instalador.")

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


def _ensure_docker_daemon_running():
    """
    Ensures the isolated Docker daemon is running.
    If the socket doesn't exist, attempts to start the daemon.
    """
    socket_path = DOCKER_SOCKET
    if not socket_path:
        return True

    if os.path.exists(socket_path):
        return True

    main_dir = config.get("main.MAIN_DIR")
    start_script = os.path.join(main_dir, "bash", "start_docker_daemon.sh")

    if os.path.exists(start_script):
        try:
            result = subprocess.run(
                ["/bin/bash", start_script, main_dir],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False
    return False


def _create_docker_client():
    """Creates a Docker client connected to nodo's isolated daemon."""
    socket_path = DOCKER_SOCKET
    _ensure_docker_daemon_running()
    return docker_lib.DockerClient(
        base_url=f"unix://{socket_path}",
        timeout=config.get("virtualizers.docker.DOCKER_CLIENT_TIMEOUT", 480),
        max_pool_size=config.get("virtualizers.docker.DOCKER_MAX_CONNECTIONS", 1000),
    )


DOCKER_CLIENT = _create_docker_client

DEFAULT_SYSTEM_RESOURCES: celaut_pb2.Sysresources = celaut_pb2.Sysresources(
    mem_limit=50 * pow(10, 6),
)
