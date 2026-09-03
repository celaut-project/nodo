#!/bin/bash
# Build the initramfs the guest boots into, as a publishable release asset.
#
#   bash/guest-kernel/build-initramfs.sh <arm64|x86_64> <output-dir> <busybox>
#
# Produces <output-dir>/initramfs-linux-{arm64,amd64} plus its .sha256, published
# alongside the guest kernel and busybox. Building it here rather than on each host
# is what stops the guest from being a function of whatever cpio, gzip and umask a
# node happens to have.
#
# <busybox> is passed in rather than found, because which busybox goes inside the
# image is the caller's decision and it has to match the one published under the
# same tag: the audit story is "rebuild this commit against the pinned busybox and
# compare digests", and that only holds if the shipped image contains exactly that
# binary. The kernel is the other half of the same guest but is not an input here --
# the initramfs loads no modules, so it is kernel-version independent.
#
# Native builds only, like its siblings, even though this only repacks a binary
# someone else compiled: build_ch_initramfs.sh runs `busybox --list` to verify the
# applets /init calls, and a foreign binary cannot execute. Without this guard the
# failure surfaces as "'busybox --list' produced no output", which says nothing
# about the actual problem.
set -euo pipefail

fail() { echo "Error: $1" >&2; exit 1; }

TARGET_ARCH="${1:-}"
OUTPUT_DIR="${2:-}"
BUSYBOX_PATH="${3:-}"
[ -n "$TARGET_ARCH" ] && [ -n "$OUTPUT_DIR" ] && [ -n "$BUSYBOX_PATH" ] \
    || fail "Usage: $0 <arm64|x86_64> <output-dir> <busybox>"

case "$TARGET_ARCH" in
    arm64)  ASSET="initramfs-linux-arm64"; ARCH_TAG="linux/arm64" ;;
    x86_64) ASSET="initramfs-linux-amd64"; ARCH_TAG="linux/amd64" ;;
    *)      fail "Unsupported architecture '$TARGET_ARCH' (expected arm64 or x86_64)." ;;
esac

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH:$TARGET_ARCH" in
    aarch64:arm64|arm64:arm64|x86_64:x86_64) ;;
    *) fail "Cross-building is not supported: host is $HOST_ARCH, target is $TARGET_ARCH.
The applet check has to execute the busybox going into the image." ;;
esac

[ -f "$BUSYBOX_PATH" ] || fail "No busybox at $BUSYBOX_PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILDER="$SCRIPT_DIR/../build_ch_initramfs.sh"
[ -f "$BUILDER" ] || fail "Missing initramfs builder at $BUILDER"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# build_ch_initramfs.sh reads busybox from a TARGET_DIR-shaped tree and accepts no
# other source, so stage the binary where it expects to find it.
STAGE="$WORKDIR/stage"
mkdir -p "$STAGE/cloud_hypervisor/busybox/$ARCH_TAG"
install -m 0755 "$BUSYBOX_PATH" "$STAGE/cloud_hypervisor/busybox/$ARCH_TAG/busybox"

echo "Building $ASSET from $(basename "$BUSYBOX_PATH")..."
bash "$BUILDER" "$STAGE" "$ARCH_TAG" "$WORKDIR/$ASSET"

# The builder is byte-reproducible by construction; prove it here instead of
# trusting it. Without this a change that reintroduces build-time variance (a
# stray umask dependency, an unpinned mtime) would only surface much later, as a
# digest nobody can reproduce from the commit it claims to come from.
bash "$BUILDER" "$STAGE" "$ARCH_TAG" "$WORKDIR/$ASSET.again" >/dev/null
cmp "$WORKDIR/$ASSET" "$WORKDIR/$ASSET.again" \
    || fail "initramfs build is not reproducible: two builds of the same inputs differ."
echo "Reproducibility check OK (two builds are byte-identical)."

mkdir -p "$OUTPUT_DIR"
install -m 0644 "$WORKDIR/$ASSET" "$OUTPUT_DIR/$ASSET"
( cd "$OUTPUT_DIR" && sha256sum "$ASSET" > "$ASSET.sha256" )

echo "Built $ASSET: $(du -h "$OUTPUT_DIR/$ASSET" | cut -f1)"
cat "$OUTPUT_DIR/$ASSET.sha256"
