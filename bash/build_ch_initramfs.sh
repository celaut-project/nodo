#!/bin/bash
set -euo pipefail

TARGET_DIR="${1:-}"
ARCH_TAG="${2:-}"
OUTPUT_PATH="${3:-}"

fail() {
    echo "Error: $1" >&2
    exit 1
}

if [ -z "$TARGET_DIR" ] || [ -z "$ARCH_TAG" ] || [ -z "$OUTPUT_PATH" ]; then
    fail "Usage: $0 <TARGET_DIR> <ARCH_TAG> <OUTPUT_PATH>"
fi
if [ ! -d "$TARGET_DIR" ]; then
    fail "TARGET_DIR does not exist: $TARGET_DIR"
fi

case "$ARCH_TAG" in
    linux/amd64|linux/arm64)
        ;;
    *)
        fail "Unsupported ARCH_TAG '$ARCH_TAG' (expected linux/amd64 or linux/arm64)."
        ;;
esac

BUSYBOX_BIN="$(command -v busybox || true)"
if [ -z "$BUSYBOX_BIN" ]; then
    fail "busybox binary not found in PATH. Install busybox-static."
fi
if ! command -v ldd >/dev/null 2>&1; then
    fail "ldd is required to validate that busybox is static."
fi

if ldd "$BUSYBOX_BIN" 2>&1 | grep -vq "not a dynamic executable"; then
    fail "busybox must be static for initramfs usage. Install busybox-static."
fi

if ! command -v cpio >/dev/null 2>&1; then
    fail "cpio is required to build initramfs."
fi
if ! command -v gzip >/dev/null 2>&1; then
    fail "gzip is required to build initramfs."
fi

WORKDIR="$(mktemp -d)"
ROOT="$WORKDIR/root"
cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

mkdir -p "$ROOT/bin" "$ROOT/dev" "$ROOT/etc" "$ROOT/newroot" "$ROOT/proc" "$ROOT/sys"

install -m 0755 "$BUSYBOX_BIN" "$ROOT/bin/busybox"
for applet in sh mount switch_root sleep cat echo mkdir ln test; do
    ln -sf /bin/busybox "$ROOT/bin/$applet"
done

cat > "$ROOT/init" <<'INIT_EOF'
#!/bin/sh
set -eu

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

log() {
    echo "[nodo-ch-initramfs] $*"
}

fatal() {
    log "ERROR: $*"
    while true; do
        sleep 3600
    done
}

mkdir -p /proc /sys /dev /newroot
mount -t proc proc /proc || fatal "cannot mount /proc"
mount -t sysfs sysfs /sys || fatal "cannot mount /sys"
mount -t devtmpfs devtmpfs /dev || mount -t tmpfs tmpfs /dev || fatal "cannot mount /dev"

WAIT_SECONDS=20
i=0
while [ "$i" -lt "$WAIT_SECONDS" ]; do
    if [ -b /dev/vda ]; then
        break
    fi
    i=$((i + 1))
    sleep 1
done
[ -b /dev/vda ] || fatal "timed out waiting for /dev/vda after ${WAIT_SECONDS}s"

mount -t ext4 -o rw /dev/vda /newroot || fatal "cannot mount /dev/vda on /newroot"

[ -f /newroot/__config__ ] || fatal "missing /__config__ in service rootfs"
[ -f /newroot/.__nodo_entrypoint ] || fatal "missing /.__nodo_entrypoint metadata file"

ENTRYPOINT=""
if ! IFS= read -r ENTRYPOINT < /newroot/.__nodo_entrypoint; then
    ENTRYPOINT=""
fi
[ -n "$ENTRYPOINT" ] || fatal "empty entrypoint in /.__nodo_entrypoint"

case "$ENTRYPOINT" in
    /*) ;;
    *) fatal "entrypoint must be absolute, got '$ENTRYPOINT'" ;;
esac

[ -x "/newroot$ENTRYPOINT" ] || fatal "entrypoint is not executable: $ENTRYPOINT"

log "switch_root -> $ENTRYPOINT"
exec switch_root /newroot "$ENTRYPOINT"
fatal "switch_root returned unexpectedly"
INIT_EOF
chmod 0755 "$ROOT/init"

printf 'nodo-ch-initramfs:v1\narch:%s\n' "$ARCH_TAG" > "$ROOT/etc/nodo-ch-initramfs.marker"

mkdir -p "$(dirname "$OUTPUT_PATH")"
(
    cd "$ROOT"
    find . -mindepth 1 -print0 \
        | sort -z \
        | cpio --null -o --format=newc 2>/dev/null \
        | gzip -9 > "$OUTPUT_PATH"
)
chmod 0644 "$OUTPUT_PATH"

if command -v lsinitramfs >/dev/null 2>&1; then
    listing="$(lsinitramfs "$OUTPUT_PATH")"
    printf '%s\n' "$listing" | grep -qx 'init' || fail "generated initramfs misses /init"
    printf '%s\n' "$listing" | grep -qx 'bin/busybox' || fail "generated initramfs misses /bin/busybox"
    printf '%s\n' "$listing" | grep -qx 'etc/nodo-ch-initramfs.marker' || fail "generated initramfs misses marker"
fi

echo "Generated Cloud Hypervisor initramfs: $OUTPUT_PATH"
