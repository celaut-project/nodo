#!/bin/bash
# Build the static busybox that becomes the guest's entire userspace.
#
#   bash/guest-kernel/build-busybox.sh <arm64|x86_64> <output-dir>
#
# Produces <output-dir>/busybox-linux-{arm64,amd64} plus its .sha256, published
# alongside the guest kernel. The initramfs is still built on the host (its /init
# is part of the contract with src/virtualizers/ch/execute.py and must travel with
# the code), but its binary no longer does: distros compile different applet sets
# and versions, so the guest userspace used to differ per node.
#
# Runs unchanged in CI and by hand. Native builds only.
set -euo pipefail

# Bump both together. Checksum from https://busybox.net/downloads/
BUSYBOX_VERSION="${BUSYBOX_VERSION:-1.37.0}"
BUSYBOX_SHA256="${BUSYBOX_SHA256:-3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4}"

fail() { echo "Error: $1" >&2; exit 1; }

TARGET_ARCH="${1:-}"
OUTPUT_DIR="${2:-}"
[ -n "$TARGET_ARCH" ] && [ -n "$OUTPUT_DIR" ] || fail "Usage: $0 <arm64|x86_64> <output-dir>"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLET_FILE="$SCRIPT_DIR/applets.txt"
[ -f "$APPLET_FILE" ] || fail "Missing applet list at $APPLET_FILE"

case "$TARGET_ARCH" in
    arm64)  ASSET="busybox-linux-arm64" ;;
    x86_64) ASSET="busybox-linux-amd64" ;;
    *)      fail "Unsupported architecture '$TARGET_ARCH' (expected arm64 or x86_64)." ;;
esac

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH:$TARGET_ARCH" in
    aarch64:arm64|arm64:arm64|x86_64:x86_64) ;;
    *) fail "Cross-building is not supported: host is $HOST_ARCH, target is $TARGET_ARCH." ;;
esac

for tool in curl tar bzip2 make gcc; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required to build busybox."
done

# Static linking needs libc.a, which not every distro installs with its compiler:
# Debian's libc6-dev ships it, Fedora splits it into glibc-static. Without this
# check the build runs for a minute and then fails inside busybox's trylink
# wrapper, which swallows the linker error.
if ! echo 'int main(void){return 0;}' | gcc -static -x c - -o /dev/null 2>/dev/null; then
    fail "This toolchain cannot link statically (missing libc.a).
Install it: 'glibc-static' (+ 'libxcrypt-static') on Fedora/RHEL, 'libc6-dev' on Debian/Ubuntu."
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

TARBALL="busybox-${BUSYBOX_VERSION}.tar.bz2"
URL="https://busybox.net/downloads/${TARBALL}"

echo "Downloading ${URL}..."
curl -fsSL "$URL" -o "$WORKDIR/$TARBALL"
actual="$(sha256sum "$WORKDIR/$TARBALL" | awk '{print $1}')"
[ "$actual" = "$BUSYBOX_SHA256" ] \
    || fail "busybox tarball SHA256 mismatch: expected $BUSYBOX_SHA256, got $actual"
echo "busybox tarball checksum OK."

tar -xf "$WORKDIR/$TARBALL" -C "$WORKDIR"
cd "$WORKDIR/busybox-${BUSYBOX_VERSION}"

make defconfig >/dev/null

# Static: the initramfs has no libc, no loader and no /lib at all — a dynamically
# linked busybox cannot even start, and the guest panics before /init runs.
sed -i 's/^# CONFIG_STATIC is not set$/CONFIG_STATIC=y/' .config
# tc(8) does not build against current kernel headers and nothing here uses it.
sed -i 's/^CONFIG_TC=y$/# CONFIG_TC is not set/' .config
# busybox 1.37.0 defconfig enables the SHA hardware-accelerated paths on every
# architecture, but their implementation is x86-only: on aarch64 the build dies
# with "sha1_process_block64_shaNI undeclared". Nothing in /init hashes anything.
sed -i 's/^CONFIG_SHA1_HWACCEL=y$/# CONFIG_SHA1_HWACCEL is not set/' .config
sed -i 's/^CONFIG_SHA256_HWACCEL=y$/# CONFIG_SHA256_HWACCEL is not set/' .config
make oldconfig >/dev/null 2>&1

grep -q '^CONFIG_STATIC=y$' .config || fail "CONFIG_STATIC did not survive oldconfig."

echo "Building busybox ${BUSYBOX_VERSION} with -j$(nproc)..."
make -j"$(nproc)" >/dev/null

[ -f busybox ] || fail "Build produced no busybox binary."

if ldd ./busybox 2>&1 | grep -vq "not a dynamic executable"; then
    fail "built busybox is dynamically linked; it would not run inside the initramfs."
fi

# The applet set is a build-time choice, so verify it here rather than discovering
# a missing command when a guest boots. Same list the initramfs symlinks.
missing=()
applet_list="$(./busybox --list)"
while IFS= read -r applet; do
    case "$applet" in ''|'#'*) continue ;; esac
    printf '%s\n' "$applet_list" | grep -qx "$applet" || missing+=("$applet")
done < "$APPLET_FILE"
[ "${#missing[@]}" -eq 0 ] || fail "built busybox lacks applets required by /init: ${missing[*]}"
echo "Applet assertions OK ($(printf '%s\n' "$applet_list" | wc -l) applets built)."

mkdir -p "$OUTPUT_DIR"
install -m 0755 ./busybox "$OUTPUT_DIR/$ASSET"
( cd "$OUTPUT_DIR" && sha256sum "$ASSET" > "$ASSET.sha256" )

echo "Built $ASSET (busybox ${BUSYBOX_VERSION}): $(du -h "$OUTPUT_DIR/$ASSET" | cut -f1)"
cat "$OUTPUT_DIR/$ASSET.sha256"
