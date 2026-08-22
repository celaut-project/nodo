"""On-demand management of nodo's optional, rootless build dependency.

A builder is only needed by the local packer (``packer.local: true``). It is
**not** installed as part of the node installation: ``bash/install_buildkit.sh``
provisions a rootless BuildKit toolchain (buildkitd + buildctl + buildkit-runc)
under ``MAIN_DIR`` on demand, the first time a local pack needs it.

This module also owns the per-pack builder lifecycle: buildkitd is started right
before a local pack and stopped right after it, so nodo never leaves a builder
running between packs.

Everything here is **unprivileged**. The builder runs as the invoking user under
rootlesskit, so starting and stopping it never needs sudo — which is the whole
point: the previous root dockerd could not be signalled by an unprivileged pack,
so a failed build left it wedged and every later pack died on its data-root lock.
The only step that may need sudo is ``install_buildkit.sh``, once, to provision
the host prerequisites (uidmap, rootlesskit, subuid/subgid ranges).
"""
import os
import subprocess
from typing import Optional

from src.utils.config import ConfigManager
from src.utils.buildkit_env import buildkit_binaries_installed


class BuilderDependencyMissing(RuntimeError):
    """Raised when the local packer needs a builder but it can't be provisioned."""


def _detect_main_dir() -> str:
    try:
        main_dir = ConfigManager().get("MAIN_DIR")
        if main_dir:
            return str(main_dir)
    except Exception:
        pass
    return os.getcwd()


def get_builder_install_command() -> str:
    main_dir = _detect_main_dir()
    return f"/bin/bash {main_dir}/bash/install_buildkit.sh {main_dir}"


def _bash_script_path(name: str) -> str:
    return os.path.join(_detect_main_dir(), "bash", name)


def ensure_builder_installed(feature: Optional[str] = "the local packer") -> None:
    """Provision nodo's rootless BuildKit toolchain if it isn't present yet.

    No-op when the binaries already exist. Runs ``bash/install_buildkit.sh`` (the
    same on-demand pattern as ``install_java.sh``) otherwise, and raises
    :class:`BuilderDependencyMissing` if the install fails or the binaries are
    still missing afterwards.
    """
    if buildkit_binaries_installed():
        return

    install_cmd = get_builder_install_command()
    feature_text = f" required by {feature}" if feature else ""
    print(
        f"nodo's rootless builder (BuildKit){feature_text} is not installed. "
        f"Installing it now:\n  {install_cmd}\n"
        "Provisioning the host prerequisites for rootless builds may ask for sudo "
        "once; packing itself never does."
    )
    result = subprocess.run(install_cmd, shell=True)
    if result.returncode != 0 or not buildkit_binaries_installed():
        raise BuilderDependencyMissing(
            "Failed to install nodo's rootless BuildKit toolchain. Install it "
            f"manually with `{install_cmd}` and re-run `nodo pack`."
        )


def start_builder(timeout: int = 90) -> None:
    """Start nodo's rootless BuildKit builder (idempotent)."""
    main_dir = _detect_main_dir()
    script = _bash_script_path("start_buildkit_daemon.sh")
    result = subprocess.run(["/bin/bash", script, main_dir], timeout=timeout)
    if result.returncode != 0:
        raise BuilderDependencyMissing(
            f"Could not start nodo's rootless BuildKit builder (see {script} output above)."
        )


def stop_builder(timeout: int = 60) -> None:
    """Stop nodo's rootless BuildKit builder. Best-effort — never raises.

    A non-zero exit means the builder survived the shutdown. It runs as our own
    user, so that should not happen; surface it rather than swallowing it, since a
    leftover builder holds its root lock and the next pack has to clear it.
    """
    main_dir = _detect_main_dir()
    script = _bash_script_path("stop_buildkit_daemon.sh")
    try:
        result = subprocess.run(["/bin/bash", script, main_dir], timeout=timeout, check=False)
        if result.returncode != 0:
            print(
                "Warning: nodo's rootless BuildKit builder could not be stopped "
                "(see output above). Stop it manually with:\n"
                f'    /bin/bash "{script}" "{main_dir}"'
            )
    except Exception as exc:  # pragma: no cover - cleanup best effort
        print(f"Warning: failed to stop nodo's rootless BuildKit builder: {exc}")
