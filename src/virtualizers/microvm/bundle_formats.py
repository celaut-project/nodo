"""What filesystem a built rootfs image holds, and what it is called on disk.

Its own module because three layers need the same answer and none of them should
have to import the others to get it: ``build`` writes the image, ``bundle`` reads
the manifest back, and each hypervisor's launch path turns the format into a
kernel cmdline and a mount type. Putting these in ``build`` would make every
launch import the builder -- the block registry, the packer's hashing, the whole
tree-writing path -- to learn a string.

Deliberately dependency-free, for the same reason ``initramfs`` is: stdlib only.
"""
from typing import Any, Mapping

# The writable default. Every service packed before ``read_mode`` existed is one.
ROOTFS_FORMAT_EXT4 = "ext4"
ROOTFS_FORMAT_SQUASHFS = "squashfs"
ROOTFS_FORMAT_EROFS = "erofs"

# ``rootfs.ext4`` keeps its name for ext4 alone. It is what every bundle written
# before this existed is called, and what the runtime copy, the prune sweep and
# both execute paths look for; renaming it would orphan every service already
# built on a node that upgrades. The read-only formats get names of their own so
# that a directory never holds two images claiming to be the rootfs.
ROOTFS_IMAGE_NAMES = {
    ROOTFS_FORMAT_EXT4: "rootfs.ext4",
    ROOTFS_FORMAT_SQUASHFS: "rootfs.squashfs",
    ROOTFS_FORMAT_EROFS: "rootfs.erofs",
}

# Formats that can only ever be mounted read-only. Not a policy choice: neither
# squashfs nor erofs has a writable implementation in the kernel at all.
READ_ONLY_ROOTFS_FORMATS = frozenset({ROOTFS_FORMAT_SQUASHFS, ROOTFS_FORMAT_EROFS})
SUPPORTED_ROOTFS_FORMATS = frozenset(ROOTFS_IMAGE_NAMES)


def rootfs_format_of(bundle: Mapping[str, Any]) -> str:
    """Which filesystem this bundle's image holds.

    Absent is ext4, and has to be: a bundle written before the key existed carries
    no other evidence of its format, and defaulting any other way would stop an
    upgraded node booting everything it had already built.
    """
    return str(bundle.get("rootfs_format") or ROOTFS_FORMAT_EXT4).strip().lower()


def is_read_only_format(rootfs_format: str) -> bool:
    """Whether an image of this format can only be mounted read-only.

    The single place the question is answered, because two callers ask it about
    the same image and have to agree: ``execute`` builds the kernel cmdline from
    it (``ro`` vs ``rw``) and the guest's ``/init`` mounts by it. Disagreeing
    leaves a guest in a mount that fails inside the initramfs, with nothing on the
    console to say why.
    """
    return rootfs_format in READ_ONLY_ROOTFS_FORMATS
