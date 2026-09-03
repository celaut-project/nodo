#!/bin/bash
# Build the Nodo guest kernel for one architecture.
#
#   bash/guest-kernel/build.sh <arm64|x86_64> <output-dir>
#
# Produces <output-dir>/vmlinuz-linux-{arm64,amd64} plus its .sha256. This is the
# kernel every service microVM boots: it is downloaded from the guest-kernel
# release at install time, never taken from the host's /boot (a distro kernel is
# large, unpredictable, and — with CONFIG_EFI_ZBOOT — not even loadable by
# Cloud Hypervisor).
#
# Runs unchanged in CI (.github/workflows/guest-kernel.yml) and by hand. Native
# builds only: run it on a host of the target architecture.
set -euo pipefail

# Bump both together. The checksum is the one published in
# https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc
KERNEL_VERSION="${KERNEL_VERSION:-6.12.103}"
KERNEL_SHA256="${KERNEL_SHA256:-f143aaade8877ba5616e788b4482576db28481bcf557ef537f4fcc3938fc3176}"

fail() { echo "Error: $1" >&2; exit 1; }

TARGET_ARCH="${1:-}"
OUTPUT_DIR="${2:-}"
[ -n "$TARGET_ARCH" ] && [ -n "$OUTPUT_DIR" ] || fail "Usage: $0 <arm64|x86_64> <output-dir>"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$TARGET_ARCH" in
    arm64)
        ARCH_TAG="linux/arm64"; ASSET="vmlinuz-linux-arm64"
        KBUILD_ARCH="arm64"; KBUILD_IMAGE="arch/arm64/boot/Image"
        ;;
    x86_64)
        ARCH_TAG="linux/amd64"; ASSET="vmlinuz-linux-amd64"
        KBUILD_ARCH="x86_64"; KBUILD_IMAGE="arch/x86/boot/bzImage"
        ;;
    *) fail "Unsupported architecture '$TARGET_ARCH' (expected arm64 or x86_64)." ;;
esac

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH:$TARGET_ARCH" in
    aarch64:arm64|arm64:arm64|x86_64:x86_64) ;;
    *) fail "Cross-building is not supported: host is $HOST_ARCH, target is $TARGET_ARCH." ;;
esac

for tool in curl tar xz make gcc flex bison bc; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required to build the guest kernel."
done

# The kernel tree plus objects needs ~15 GB. mktemp honours TMPDIR, so point it at
# a disk-backed directory when /tmp is a small tmpfs (the default on Fedora, where
# an unset TMPDIR fails the build with "Disk quota exceeded" mid-compile).
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

avail_kb="$(df -Pk "$WORKDIR" | awk 'NR==2 {print $4}')"
[ "${avail_kb:-0}" -ge 15000000 ] \
    || fail "Only $((avail_kb / 1024)) MiB free in $(dirname "$WORKDIR"); the build needs ~15 GB. Set TMPDIR to a larger filesystem."

MAJOR="${KERNEL_VERSION%%.*}"
TARBALL="linux-${KERNEL_VERSION}.tar.xz"
URL="https://cdn.kernel.org/pub/linux/kernel/v${MAJOR}.x/${TARBALL}"

echo "Downloading ${URL}..."
curl -fsSL "$URL" -o "$WORKDIR/$TARBALL"

if [ -n "$KERNEL_SHA256" ]; then
    actual="$(sha256sum "$WORKDIR/$TARBALL" | awk '{print $1}')"
    [ "$actual" = "$KERNEL_SHA256" ] \
        || fail "Kernel tarball SHA256 mismatch: expected $KERNEL_SHA256, got $actual"
    echo "Kernel tarball checksum OK."
else
    echo "WARNING: KERNEL_SHA256 is empty; skipping tarball verification." >&2
fi

tar -xf "$WORKDIR/$TARBALL" -C "$WORKDIR"
SRC="$WORKDIR/linux-${KERNEL_VERSION}"

cd "$SRC"
echo "Configuring (defconfig + kvm_guest.config + Nodo fragments)..."
make ARCH="$KBUILD_ARCH" defconfig >/dev/null
make ARCH="$KBUILD_ARCH" kvm_guest.config >/dev/null
ARCH="$KBUILD_ARCH" scripts/kconfig/merge_config.sh -m .config \
    "$SCRIPT_DIR/nodo-guest.config" \
    "$SCRIPT_DIR/nodo-guest-${TARGET_ARCH}.config" >/dev/null
make ARCH="$KBUILD_ARCH" olddefconfig >/dev/null

# A fragment entry is a request, not a guarantee: Kconfig drops any symbol whose
# dependencies are unmet, silently. Fail the build instead of shipping a kernel
# that cannot mount a rootfs or reach the network.
assert_config() {
    local symbol="$1" expected="$2"
    if [ "$expected" = "n" ]; then
        ! grep -q "^${symbol}=" .config \
            || fail "$symbol must be disabled but the resolved .config sets it."
    else
        grep -q "^${symbol}=${expected}$" .config \
            || fail "$symbol=${expected} was requested but the resolved .config does not have it."
    fi
}
for symbol in CONFIG_VIRTIO_BLK CONFIG_VIRTIO_NET CONFIG_VIRTIO_PCI CONFIG_VIRTIO_FS \
              CONFIG_FUSE_FS CONFIG_OVERLAY_FS CONFIG_EXT4_FS CONFIG_DEVTMPFS_MOUNT \
              CONFIG_BLK_DEV_INITRD CONFIG_VETH CONFIG_BRIDGE CONFIG_NF_NAT CONFIG_SECCOMP; do
    assert_config "$symbol" y
done
assert_config CONFIG_MODULES n
# CMA would reserve 32 MiB of every guest — fatal at MIN_MEM_MIB-sized microVMs.
assert_config CONFIG_CMA n
if [ "$TARGET_ARCH" = "arm64" ]; then
    assert_config CONFIG_SERIAL_AMBA_PL011_CONSOLE y
    assert_config CONFIG_EFI_ZBOOT n
else
    assert_config CONFIG_SERIAL_8250_CONSOLE y
fi
echo "Config assertions OK."

echo "Building ${KBUILD_IMAGE} with -j$(nproc)..."
make ARCH="$KBUILD_ARCH" -j"$(nproc)" "$(basename "$KBUILD_IMAGE")" >/dev/null

[ -f "$KBUILD_IMAGE" ] || fail "Build produced no $KBUILD_IMAGE"

# Cloud Hypervisor's aarch64 loader only accepts a raw arm64 Image: "ARM\x64" at
# offset 56. Anything else (EFI zboot, gzip) is rejected at boot time, not here.
if [ "$TARGET_ARCH" = "arm64" ]; then
    [ "$(dd if="$KBUILD_IMAGE" bs=1 skip=56 count=4 status=none)" = "ARMd" ] \
        || fail "built Image lacks the arm64 magic; cloud-hypervisor could not load it"
fi

mkdir -p "$OUTPUT_DIR"
install -m 0644 "$KBUILD_IMAGE" "$OUTPUT_DIR/$ASSET"
( cd "$OUTPUT_DIR" && sha256sum "$ASSET" > "$ASSET.sha256" )

echo "Built $ASSET for $ARCH_TAG (linux ${KERNEL_VERSION}): $(du -h "$OUTPUT_DIR/$ASSET" | cut -f1)"
cat "$OUTPUT_DIR/$ASSET.sha256"
