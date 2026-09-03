"""What a nodo Cloud Hypervisor initramfs is, and how to read one.

The image is built by bash/build_ch_initramfs.sh, shipped as a release asset next
to the guest kernel, and read back by two callers with very different jobs:
execute.py refuses to launch on a bad one, doctor.py reports on it. Both need the
same three facts -- which entries must be present, where the marker lives, and
which contract version this checkout speaks -- so they live here rather than being
spelled twice and drifting.

Deliberately dependency-free: stdlib only, no config, no logger (importing the
logger creates the storage directory). doctor.py must stay runnable on a checkout
too broken to import the node, which is exactly when it is worth running.

Read with cpio, never lsinitramfs or lsinitrd. The gzip'd newc cpio layout is a
kernel ABI, but each distro brands its own inspector for it -- initramfs-tools
ships lsinitramfs, dracut lsinitrd, mkinitcpio lsinitcpio -- so requiring one made
launching fail outright on every non-Debian host.
"""
import gzip
import shutil
import subprocess
from typing import FrozenSet, Set, Tuple

# Bump together with the marker that bash/build_ch_initramfs.sh stamps: they are
# one version. It covers /init's contract with execute.py -- which files it expects
# in the service rootfs (`__config__`, `.__nodo_entrypoint`, `.__nodo_virtiofs`)
# and how it reads them. The image is pinned by digest while that contract lives in
# the code, so this is what keeps a pinned asset from silently outliving it.
CONTRACT_VERSION = "v1"

MARKER_PATH = "etc/nodo-ch-initramfs.marker"
MARKER_KEY = "nodo-ch-initramfs"

REQUIRED_ENTRIES: FrozenSet[str] = frozenset({"init", "bin/busybox", MARKER_PATH})


class InitramfsReadError(RuntimeError):
    """The file is not a readable nodo initramfs at all."""


def _cpio(args, payload: bytes) -> bytes:
    if not shutil.which("cpio"):
        raise InitramfsReadError("Required command not found in PATH: cpio")

    result = subprocess.run(["cpio", *args], input=payload, capture_output=True)
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        raise InitramfsReadError(
            f"cpio {' '.join(args)} failed: {stderr or '<empty>'}"
        )
    return result.stdout


def read(path: str) -> Tuple[Set[str], str]:
    """Return the entry names and the contract version of the initramfs at `path`.

    The version is "" when the marker carries no recognisable one; callers decide
    whether that is fatal. Raises InitramfsReadError if the file cannot be read as
    a gzip'd cpio archive at all.
    """
    try:
        with open(path, "rb") as f:
            payload = gzip.decompress(f.read())
    except OSError as e:
        raise InitramfsReadError(f"cannot read as gzip: {e}") from e

    listing = _cpio(["-t", "--quiet"], payload).decode(errors="replace")
    entries = {
        line.strip().lstrip("./") for line in listing.splitlines() if line.strip()
    }

    version = ""
    if MARKER_PATH in entries:
        marker = _cpio(["-i", "--to-stdout", "--quiet", MARKER_PATH], payload)
        for line in marker.decode(errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == MARKER_KEY:
                version = value.strip()
                break

    return entries, version


def missing_entries(entries: Set[str]) -> list:
    return sorted(REQUIRED_ENTRIES.difference(entries))
