"""On-demand management of nodo's optional, isolated Docker dependency.

Docker is only needed by the local packer (``packer.local: true``). Mirroring
``java_dependency`` / ``install_java.sh``, Docker is **not** installed as part of
the node installation: ``bash/install_docker.sh`` provisions an isolated Docker
toolchain (dockerd + docker client + buildx) under ``MAIN_DIR`` on demand, the
first time a local pack needs it — regardless of any Docker already on the host.

This module also owns the per-pack daemon lifecycle Josemi asked for: the
isolated ``dockerd`` is started right before a local pack and stopped right after
it, so nodo never leaves a Docker daemon running between packs.
"""
import os
import subprocess
from typing import Optional

from src.utils.config import ConfigManager
from src.utils.docker_env import docker_binaries_installed


class DockerDependencyMissing(RuntimeError):
    """Raised when the local packer needs Docker but it can't be provisioned."""


def _detect_main_dir() -> str:
    try:
        main_dir = ConfigManager().get("MAIN_DIR")
        if main_dir:
            return str(main_dir)
    except Exception:
        pass
    return os.getcwd()


def get_docker_install_command() -> str:
    main_dir = _detect_main_dir()
    return f"/bin/bash {main_dir}/bash/install_docker.sh {main_dir}"


def _bash_script_path(name: str) -> str:
    return os.path.join(_detect_main_dir(), "bash", name)


def ensure_docker_installed(feature: Optional[str] = "the local packer") -> None:
    """Provision nodo's isolated Docker toolchain if it isn't present yet.

    No-op when the binaries already exist. Runs ``bash/install_docker.sh`` (the
    same on-demand pattern as ``install_java.sh``) otherwise, and raises
    :class:`DockerDependencyMissing` if the install fails or the binaries are
    still missing afterwards.
    """
    if docker_binaries_installed():
        return

    install_cmd = get_docker_install_command()
    feature_text = f" required by {feature}" if feature else ""
    print(
        f"Docker (nodo's isolated toolchain){feature_text} is not installed. "
        f"Installing it now:\n  {install_cmd}"
    )
    result = subprocess.run(install_cmd, shell=True)
    if result.returncode != 0 or not docker_binaries_installed():
        raise DockerDependencyMissing(
            "Failed to install nodo's isolated Docker toolchain. Install it "
            f"manually with `{install_cmd}` and re-run `nodo pack`."
        )


def start_docker_daemon(timeout: int = 90) -> None:
    """Start nodo's isolated Docker daemon (idempotent)."""
    main_dir = _detect_main_dir()
    script = _bash_script_path("start_docker_daemon.sh")
    result = subprocess.run(["/bin/bash", script, main_dir], timeout=timeout)
    if result.returncode != 0:
        raise DockerDependencyMissing(
            f"Could not start nodo's isolated Docker daemon (see {script} output above)."
        )


def stop_docker_daemon(timeout: int = 60) -> None:
    """Stop nodo's isolated Docker daemon. Best-effort — never raises."""
    main_dir = _detect_main_dir()
    script = _bash_script_path("stop_docker_daemon.sh")
    try:
        subprocess.run(["/bin/bash", script, main_dir], timeout=timeout, check=False)
    except Exception as exc:  # pragma: no cover - cleanup best effort
        print(f"Warning: failed to stop nodo's isolated Docker daemon: {exc}")
