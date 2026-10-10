"""Can this host run the virtiofsd the node starts for a shared filesystem?

The node serves each share with one daemon, started with the flags of the Rust
``virtiofsd`` (``build_virtiofsd_command`` in ``virtiofs.py``). On a Linux host
two other things answer to that name, and neither can serve a share:

- nothing at all. No distro ships the Rust daemon on Ubuntu 22.04, and the node
  runs as root, so a binary in an operator's ``~/.cargo/bin`` is not on its PATH;
- the old C daemon from QEMU (``/usr/lib/qemu/virtiofsd``, qemu-system-common up
  to 7.x). It takes ``-o source=`` instead of ``--shared-dir`` and stops at the
  first flag the node gives it.

Both used to show only at launch, after the client was charged and the rootfs was
being built (#478). This module gives the answer before that, to ``nodo doctor``
and to ``launch_service``. It reads no database and builds nothing, so the probe
costs one ``--version`` call and only services that declare shares pay it.
"""
import os
import re
import shutil
import subprocess
from typing import NamedTuple, Optional

from src.virtualizers.microvm.errors import MicroVMError

CONFIG_KEY = "virtualizers.ch.VIRTIOFSD_BINARY"
DEFAULT_BINARY = "virtiofsd"

# Where the old C daemon lives on Debian/Ubuntu (qemu-system-common).
LEGACY_QEMU_PATH = "/usr/lib/qemu/virtiofsd"

OK = "ok"
MISSING = "missing"
LEGACY = "legacy"
UNUSABLE = "unusable"
TOO_OLD = "too_old"

# The oldest Rust virtiofsd that has every flag the node gives it. --readonly,
# for the daemon of a guest that mounts a share read-only, came in 1.13.0. An
# older daemon stops at that flag, and the guest that waits for it never boots.
MIN_RUST_VERSION = (1, 13, 0)

# Rust: "virtiofsd 1.14.0". C: "virtiofsd version 6.2.0 (Debian ...)", then a
# copyright line and "using FUSE kernel interface version 7.31".
_RUST_VERSION = re.compile(r"^virtiofsd\s+v?(\d+(?:\.\d+)+)\s*$")
_LEGACY_VERSION = re.compile(r"^virtiofsd version\s+(\S+)")

INSTALL_HINT = (
    "Re-run the installer, which puts the Rust virtiofsd in <MAIN_DIR>/bin, or "
    "install it by hand ('cargo install virtiofsd --locked', needs libcap-ng-dev "
    "and libseccomp-dev) and give the node its absolute path: "
    f"'sudo nodo config set {CONFIG_KEY}=/absolute/path/to/virtiofsd'. "
    "See docs/SHARED_FILESYSTEMS.md."
)


class VirtiofsdUnavailable(MicroVMError):
    """The configured virtiofsd is missing, or it is not the Rust daemon."""


class VirtiofsdProbe(NamedTuple):
    status: str               # OK, MISSING, LEGACY, UNUSABLE or TOO_OLD
    configured: str           # the value of virtualizers.ch.VIRTIOFSD_BINARY
    path: Optional[str]       # what it resolved to, if anything
    version: str              # as printed by --version, if known
    detail: str               # one sentence for an operator

    @property
    def usable(self) -> bool:
        return self.status == OK


def configured_binary() -> str:
    """The virtiofsd the node will start, as config.yaml names it."""
    from src.utils.config import ConfigManager

    return ConfigManager().get(CONFIG_KEY, DEFAULT_BINARY) or DEFAULT_BINARY


def probe(binary: Optional[str], timeout: float = 10) -> VirtiofsdProbe:
    """Resolve ``binary`` the way the node does, run ``--version`` and classify it."""
    configured = (binary or "").strip() or DEFAULT_BINARY
    path = shutil.which(configured)
    if not path:
        if os.path.isabs(configured):
            why = f"{configured} does not exist or is not executable"
        else:
            why = f"'{configured}' is not on the node's PATH"
            if os.access(LEGACY_QEMU_PATH, os.X_OK):
                why += (
                    f" ({LEGACY_QEMU_PATH} exists, but it is the old QEMU C "
                    "daemon, which the node cannot use)"
                )
        return VirtiofsdProbe(MISSING, configured, None, "", f"No virtiofsd: {why}.")

    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as e:
        return VirtiofsdProbe(
            UNUSABLE, configured, path, "", f"'{path} --version' did not run: {e}."
        )

    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    first_line = output.splitlines()[0].strip() if output else ""

    rust = _RUST_VERSION.match(first_line)
    if result.returncode == 0 and rust:
        version = rust.group(1)
        if _version_tuple(version) < MIN_RUST_VERSION:
            minimum = ".".join(str(n) for n in MIN_RUST_VERSION)
            return VirtiofsdProbe(
                TOO_OLD, configured, path, version,
                f"{path} is the Rust virtiofsd {version}, but the node needs "
                f"{minimum} or later. Older versions do not accept --readonly, "
                "which the node gives the daemon of a read-only share.",
            )
        return VirtiofsdProbe(
            OK, configured, path, version, f"{path} is the Rust virtiofsd {version}."
        )

    legacy = _LEGACY_VERSION.match(first_line)
    if legacy or os.path.realpath(path) == LEGACY_QEMU_PATH:
        version = legacy.group(1) if legacy else ""
        return VirtiofsdProbe(
            LEGACY, configured, path, version,
            f"{path} is the old QEMU C virtiofsd{' ' + version if version else ''}. "
            "It does not accept --socket-path/--shared-dir/--sandbox/--cache, "
            "so it cannot serve a share.",
        )

    return VirtiofsdProbe(
        UNUSABLE, configured, path, "",
        f"'{path} --version' exited {result.returncode} and printed "
        f"{first_line or 'nothing'!r}, which is not the Rust virtiofsd.",
    )


def _version_tuple(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def require_usable(binary: Optional[str] = None) -> VirtiofsdProbe:
    """The probe of the configured virtiofsd, or raise :class:`VirtiofsdUnavailable`."""
    result = probe(configured_binary() if binary is None else binary)
    if not result.usable:
        raise VirtiofsdUnavailable(
            f"this service declares shared directories, which need the Rust "
            f"virtiofsd on this node. {result.detail} {INSTALL_HINT}"
        )
    return result
