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
for applet in sh mount switch_root sleep cat echo mkdir ln test chmod ip; do
    ln -sf /bin/busybox "$ROOT/bin/$applet"
done

cat > "$ROOT/init" <<'INIT_EOF'
#!/bin/sh
set -eu

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log() {
    echo "[nodo-ch-initramfs] $*"
}

fatal() {
    log "ERROR: $*"
    while true; do
        sleep 3600
    done
}

mask_octet_to_bits() {
    case "$1" in
        255) echo 8 ;;
        254) echo 7 ;;
        252) echo 6 ;;
        248) echo 5 ;;
        240) echo 4 ;;
        224) echo 3 ;;
        192) echo 2 ;;
        128) echo 1 ;;
        0)   echo 0 ;;
        *)   return 1 ;;
    esac
}

mask_to_prefix() {
    local netmask="$1"
    local o1 o2 o3 o4
    local b1 b2 b3 b4
    IFS=. read -r o1 o2 o3 o4 <<EOF
$netmask
EOF
    [ -n "${o1:-}" ] && [ -n "${o2:-}" ] && [ -n "${o3:-}" ] && [ -n "${o4:-}" ] || return 1
    b1="$(mask_octet_to_bits "$o1")" || return 1
    b2="$(mask_octet_to_bits "$o2")" || return 1
    b3="$(mask_octet_to_bits "$o3")" || return 1
    b4="$(mask_octet_to_bits "$o4")" || return 1
    echo $((b1 + b2 + b3 + b4))
}

first_non_loopback_iface() {
    local p iface
    for p in /sys/class/net/*; do
        iface="${p##*/}"
        [ "$iface" = "lo" ] && continue
        echo "$iface"
        return 0
    done
    return 1
}

iface_has_ipv4() {
    local iface="$1"
    /bin/busybox ip -4 addr show dev "$iface" 2>/dev/null | /bin/busybox grep -q 'inet '
}

configure_guest_network() {
    local cmdline ip_arg token
    local client_ip gateway_ip netmask iface autoconf
    local old_ifs prefix

    cmdline="$(cat /proc/cmdline 2>/dev/null || true)"
    ip_arg=""
    for token in $cmdline; do
        case "$token" in
            ip=*)
                ip_arg="${token#ip=}"
                ;;
        esac
    done

    [ -n "$ip_arg" ] || {
        log "no ip= kernel parameter; skipping guest network bootstrap"
        return 0
    }

    case "$ip_arg" in
        *:*)
            old_ifs="$IFS"
            IFS=':'
            set -- $ip_arg
            IFS="$old_ifs"
            client_ip="${1:-}"
            gateway_ip="${3:-}"
            netmask="${4:-}"
            iface="${6:-}"
            autoconf="${7:-}"
            ;;
        *)
            log "unsupported ip= format '$ip_arg'; skipping guest network bootstrap"
            return 0
            ;;
    esac

    if [ -z "$iface" ] || [ "$iface" = "none" ] || [ "$iface" = "auto" ]; then
        iface="$(first_non_loopback_iface || true)"
    fi
    [ -n "$iface" ] || fatal "no guest network interface found"

    /bin/busybox ip link set dev "$iface" up || fatal "cannot bring up interface '$iface'"

    if iface_has_ipv4 "$iface"; then
        log "guest network already configured on '$iface'"
        return 0
    fi

    [ -n "$client_ip" ] || fatal "missing client IP in ip= kernel parameter"
    [ -n "$netmask" ] || fatal "missing netmask in ip= kernel parameter"
    prefix="$(mask_to_prefix "$netmask")" || fatal "invalid netmask '$netmask' in ip= kernel parameter"

    /bin/busybox ip addr add "${client_ip}/${prefix}" dev "$iface" \
        || fatal "cannot assign ${client_ip}/${prefix} to '$iface'"

    if [ -n "$gateway_ip" ] && [ "$gateway_ip" != "0.0.0.0" ]; then
        /bin/busybox ip route replace default via "$gateway_ip" dev "$iface" \
            || fatal "cannot set default route via '$gateway_ip' on '$iface'"
    fi

    log "configured guest network iface=$iface ip=${client_ip}/${prefix} gw=${gateway_ip:-<none>} autoconf=${autoconf:-<empty>}"
}

mkdir -p /proc /sys /dev /newroot
mount -t proc proc /proc || fatal "cannot mount /proc"
mount -t sysfs sysfs /sys || fatal "cannot mount /sys"
mount -t devtmpfs devtmpfs /dev || mount -t tmpfs tmpfs /dev || fatal "cannot mount /dev"
mkdir -p /dev/shm
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm || fatal "cannot mount /dev/shm"

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
configure_guest_network

mkdir -p /newroot/proc /newroot/sys /newroot/dev /newroot/run /newroot/tmp
chmod 1777 /newroot/tmp || fatal "cannot set /newroot/tmp permissions"

mount --move /proc /newroot/proc || fatal "cannot move /proc to new root"
mount --move /sys /newroot/sys || fatal "cannot move /sys to new root"
mount --move /dev /newroot/dev || fatal "cannot move /dev to new root"
mount -t tmpfs -o mode=755,nosuid,nodev tmpfs /newroot/run || fatal "cannot mount /run"

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
