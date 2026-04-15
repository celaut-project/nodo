import os
import subprocess
from pathlib import Path

import docker as docker_lib
from protos import celaut_pb2

from src.utils.config import ConfigManager
from src.utils import logger as log

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
DEFAULT_BIN_DIR = NODO_ROOT / "bin"

DOCKER_BIN = str(config.get("dependencies.docker.BIN") or (DEFAULT_BIN_DIR / "docker"))
DOCKERD_BIN = str(config.get("dependencies.docker.DAEMON_BIN") or (DEFAULT_BIN_DIR / "dockerd"))
BUILDX_BIN = str(
    config.get("dependencies.docker.BUILDX_BIN")
    or (NODO_ROOT / "libexec" / "docker" / "cli-plugins" / "docker-buildx")
)
BIN_DIR = Path(DOCKER_BIN).resolve().parent
PLUGIN_DIR = Path(BUILDX_BIN).resolve().parent
DOCKER_SOCKET = config.get("virtualizers.docker.DOCKER_SOCKET") or str(NODO_ROOT / "docker" / "docker.sock")

if not os.path.isfile(DOCKER_BIN):
    raise RuntimeError(f"Cliente Docker de Nodo no encontrado en {DOCKER_BIN}. Ejecuta el instalador.")
if not os.path.isfile(BUILDX_BIN):
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
    base_url = f"unix://{socket_path}" if socket_path else None
    try:
        log.LOGGER(f"[DOCKER] Creating DockerClient. base_url={base_url}, DOCKER_HOST_env={os.environ.get('DOCKER_HOST')}")
    except Exception:
        pass

    try:
        client = docker_lib.DockerClient(
            base_url=base_url,
            timeout=config.get("virtualizers.docker.DOCKER_CLIENT_TIMEOUT", 480),
            max_pool_size=config.get("virtualizers.docker.DOCKER_MAX_CONNECTIONS", 1000)
        )
        return client
    except Exception as e:
        err_str = str(e)
        if "Not supported URL scheme" in err_str and "http+docker" in err_str:
            advice = (
                "Docker client failed due to unsupported URL scheme 'http+docker'. "
                "Install 'requests-unixsocket' or ensure DOCKER_HOST/DOCKER_SOCKET is a valid unix or tcp URL."
            )
            log.LOGGER(f"[DOCKER] {err_str}. {advice}")
            raise RuntimeError(f"Unable to create Docker client for socket {socket_path}: {err_str}\n{advice}")
        raise


DOCKER_CLIENT = _create_docker_client

DEFAULT_SYSTEM_RESOURCES: celaut_pb2.Sysresources = celaut_pb2.Sysresources(
    mem_limit=50 * pow(10, 6),
)
