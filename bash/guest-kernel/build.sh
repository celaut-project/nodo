#!/bin/bash
# Build the Nodo guest kernel for one architecture.
#
#   bash/guest-kernel/build.sh <arm64|x86_64> <output-dir> [dev]
#
# Produces <output-dir>/vmlinuz-linux-{arm64,amd64} plus its .sha256.
#
# With "dev" (x86_64 only, EXPERIMENTAL, #536): the device variant, with loadable
# modules (nodo-guest-dev.config). It produces vmlinuz-dev-linux-amd64, the modules
# built in the tree (kernel-dev-modules-linux-amd64.tar.xz) and the tree that a
# service builds its own modules against (kernel-dev-build-linux-amd64.tar.xz:
# headers, .config, Module.symvers), each with its .sha256. This is the
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
VARIANT="${3:-}"
[ -n "$TARGET_ARCH" ] && [ -n "$OUTPUT_DIR" ] || fail "Usage: $0 <arm64|x86_64> <output-dir> [dev]"
case "$VARIANT" in
    "") ;;
    dev) [ "$TARGET_ARCH" = "x86_64" ] || fail "The dev (device) kernel is built for x86_64 only." ;;
    *) fail "Unknown variant '$VARIANT' (expected nothing or dev)." ;;
esac

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
FRAGMENTS=("$SCRIPT_DIR/nodo-guest.config" "$SCRIPT_DIR/nodo-guest-${TARGET_ARCH}.config")
[ "$VARIANT" = "dev" ] && FRAGMENTS+=("$SCRIPT_DIR/nodo-guest-dev.config")
ARCH="$KBUILD_ARCH" scripts/kconfig/merge_config.sh -m .config "${FRAGMENTS[@]}" >/dev/null
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
              CONFIG_BLK_DEV_INITRD CONFIG_VETH CONFIG_BRIDGE CONFIG_NF_NAT CONFIG_SECCOMP \
              CONFIG_SQUASHFS CONFIG_EROFS_FS CONFIG_BPF_SYSCALL CONFIG_CGROUP_BPF \
              CONFIG_IP_NF_RAW; do
    assert_config "$symbol" y
done
if [ "$VARIANT" = "dev" ]; then
    assert_config CONFIG_MODULES y
    assert_config CONFIG_DRM m
    assert_config CONFIG_FW_LOADER y
    assert_config CONFIG_MODULE_SIG n
else
    assert_config CONFIG_MODULES n
fi
# CMA would reserve 32 MiB of every guest — fatal at MIN_MEM_MIB-sized microVMs.
assert_config CONFIG_CMA n
if [ "$TARGET_ARCH" = "arm64" ]; then
    assert_config CONFIG_SERIAL_AMBA_PL011_CONSOLE y
    assert_config CONFIG_EFI_ZBOOT n
else
    assert_config CONFIG_SERIAL_8250_CONSOLE y
    # Without it, a guest sees one CPU on Cloud Hypervisor (#486).
    assert_config CONFIG_X86_X2APIC y
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
if [ "$VARIANT" = "dev" ]; then
    ASSET="vmlinuz-dev-linux-amd64"
    echo "Building the modules..."
    make ARCH="$KBUILD_ARCH" -j"$(nproc)" modules >/dev/null
    # Fixed owner and times, so a tarball differs only when its files do.
    TAR_OPTS=(--sort=name --owner=0 --group=0 --numeric-owner --mtime=@0)
    STAGE="$WORKDIR/stage"
    make ARCH="$KBUILD_ARCH" INSTALL_MOD_PATH="$STAGE/modules" INSTALL_MOD_STRIP=1 \
        modules_install >/dev/null
    rm -f "$STAGE"/modules/lib/modules/*/build "$STAGE"/modules/lib/modules/*/source
    tar "${TAR_OPTS[@]}" -C "$STAGE/modules" -cJf "$OUTPUT_DIR/kernel-dev-modules-linux-amd64.tar.xz" lib
    # The external-module build tree: what `make -C <tree> M=<dir> modules` needs.
    # Normally run by the packaging targets of the kernel Makefile, which export these.
    srctree="$SRC" SRCARCH=x86 KCONFIG_CONFIG=.config ARCH="$KBUILD_ARCH" MAKE=make \
        CC="${CC:-gcc}" HOSTCC="${HOSTCC:-gcc}" HOSTCXX="${HOSTCXX:-g++}" \
        scripts/package/install-extmod-build "$STAGE/build" >/dev/null
    cp .config Module.symvers "$STAGE/build/"
    tar "${TAR_OPTS[@]}" -C "$STAGE" -cJf "$OUTPUT_DIR/kernel-dev-build-linux-amd64.tar.xz" build
    ( cd "$OUTPUT_DIR" && for f in kernel-dev-modules-linux-amd64.tar.xz kernel-dev-build-linux-amd64.tar.xz; do
        sha256sum "$f" > "$f.sha256"; done )
fi
install -m 0644 "$KBUILD_IMAGE" "$OUTPUT_DIR/$ASSET"
( cd "$OUTPUT_DIR" && sha256sum "$ASSET" > "$ASSET.sha256" )

echo "Built $ASSET for $ARCH_TAG (linux ${KERNEL_VERSION}): $(du -h "$OUTPUT_DIR/$ASSET" | cut -f1)"
cat "$OUTPUT_DIR/$ASSET.sha256"
