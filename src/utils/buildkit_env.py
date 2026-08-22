"""Paths and environment for nodo's *rootless* BuildKit builder.

nodo does not use containers as a runtime (Cloud Hypervisor is the only
virtualizer). A builder is an **optional** dependency needed solely by the local
packer (`packer.local: true`), whose entire use of it is one call: build a
Dockerfile and export the resulting filesystem as a tar. That is BuildKit's job —
`docker buildx` was only ever a front end for it — so nodo drives BuildKit
directly.

Driving BuildKit directly is what makes the local packer **sudo-free**: buildkitd
runs as the invoking user under rootlesskit, so nodo can always start and stop it
(see bash/start_buildkit_daemon.sh). The previous isolated dockerd ran as root
via sudo, which meant an unprivileged pack could not signal it at all: a failed
build left it alive, holding its data-root lock, and blocked every later pack.

Like the module it replaces, this one never imports a container library and never
raises at import time — presence is checked lazily via
:func:`buildkit_binaries_installed`, and the packer installs the toolchain on
demand before it ever uses these values.
"""
import os
from pathlib import Path

from src.utils.config import ConfigManager

config = ConfigManager()


def _main_dir() -> Path:
    main_dir = config.get("main.MAIN_DIR")
    if main_dir:
        return Path(str(main_dir)).expanduser().resolve()
    # Fall back to the repository root (this file is src/utils/buildkit_env.py).
    return Path(__file__).resolve().parents[2]


NODO_ROOT = _main_dir()
DEFAULT_BIN_DIR = NODO_ROOT / "bin"

# Rootless BuildKit toolchain paths (overridable via dependencies.buildkit.* in config).
BUILDCTL_BIN = str(config.get("dependencies.buildkit.BIN") or (DEFAULT_BIN_DIR / "buildctl"))
BUILDKITD_BIN = str(config.get("dependencies.buildkit.DAEMON_BIN") or (DEFAULT_BIN_DIR / "buildkitd"))

BIN_DIR = Path(BUILDCTL_BIN).resolve().parent

# The rootless builder binds this private socket (see bash/start_buildkit_daemon.sh).
BUILDKIT_SOCKET = str(
    config.get("dependencies.buildkit.BUILDKIT_SOCKET")
    or (NODO_ROOT / "buildkit" / "buildkitd.sock")
)

# Environment that points any `buildctl` invocation at nodo's own builder — never
# a system-wide one. BIN_DIR must be on PATH so buildkitd finds buildkit-runc and
# the CNI helpers next to it.
BUILDKIT_ENV = os.environ.copy()
BUILDKIT_ENV.update(
    {
        "BUILDKIT_HOST": f"unix://{BUILDKIT_SOCKET}",
        "PATH": f"{BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
    }
)

BUILDCTL_COMMAND = [BUILDCTL_BIN, "--addr", f"unix://{BUILDKIT_SOCKET}"]


def buildkit_binaries_installed() -> bool:
    """True when nodo's buildkitd + buildctl are present."""
    return os.path.isfile(BUILDKITD_BIN) and os.path.isfile(BUILDCTL_BIN)
