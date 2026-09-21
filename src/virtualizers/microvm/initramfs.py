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
# in the service rootfs (`__config__`, `.__nodo_entrypoint`, `.__nodo_virtiofs`,
# `.__nodo_envs`) and how it reads them. The image is pinned by digest while that
# contract lives in the code, so this is what keeps a pinned asset from silently
# outliving it.
#
# v2 (#369): /init reads `rootfstype=` and `ro`/`rw` off the kernel cmdline instead
# of assuming ext4+rw, and on `ro` it overlays the image and takes the three
# metadata files from a second block device rather than from inside the image.
# Both halves are the same change, which is why they share a version: a v1
# initramfs handed a `ro` cmdline mounts the image ext4+rw and fails, and a v2 one
# on a ro bundle needs the /dev/vdb this checkout's execute.py attaches. Refusing
# the skew here is what turns either into one error at launch instead of a guest
# that hangs in the initramfs with nothing on its console.
#
# v3 (#405): /init additionally reads an optional `.__nodo_envs` file and
# `export`s the KEY=VALUE pairs it decodes from it into the entrypoint's own
# environment before switch_root, on top of the same values already delivered,
# unconditionally, inside __config__.config.environment_variables. A v2
# initramfs handed a bundle whose execute.py wrote a `.__nodo_envs` simply never
# reads it -- the entrypoint still boots, just without those Linux env vars --
# so this bump is about keeping /init's read half in step with execute.py's
# write half, not about a launch that would otherwise fail outright.
CONTRACT_VERSION = "v3"

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
