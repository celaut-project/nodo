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

# busybox is the guest's entire userspace, and the only input to this initramfs
# that could still come from the host. It must not: distros compile different
# applet sets and link against different libc behaviour, so a host busybox means
# every node runs services on a subtly different guest. The release-provisioned
# binary is therefore the only accepted source.
#
# NODO_ALLOW_HOST_BUSYBOX=1 exists for developers rebuilding an initramfs by hand
# on a machine with no provisioned asset. The installer never sets it, and what it
# produces is explicitly not what nodes run.
PROVISIONED_BUSYBOX="$TARGET_DIR/cloud_hypervisor/busybox/${ARCH_TAG}/busybox"
if [ -x "$PROVISIONED_BUSYBOX" ]; then
    BUSYBOX_BIN="$PROVISIONED_BUSYBOX"
elif [ "${NODO_ALLOW_HOST_BUSYBOX:-0}" = "1" ]; then
    BUSYBOX_BIN="$(command -v busybox || true)"
    if [ -z "$BUSYBOX_BIN" ]; then
        fail "NODO_ALLOW_HOST_BUSYBOX=1 but no busybox in PATH."
    fi
    echo "Warning: NODO_ALLOW_HOST_BUSYBOX=1, using the host's busybox (${BUSYBOX_BIN})." >&2
    echo "Warning: the resulting initramfs is a local dev build, not the one nodes run." >&2
else
    fail "No provisioned busybox at ${PROVISIONED_BUSYBOX}. Re-run the installer to provision it, or set NODO_ALLOW_HOST_BUSYBOX=1 for a local dev build."
fi
if ! command -v ldd >/dev/null 2>&1; then
    fail "ldd is required to validate that busybox is static."
fi

if ldd "$BUSYBOX_BIN" 2>&1 | grep -vq "not a dynamic executable"; then
    fail "busybox must be static for initramfs usage. Install busybox-static."
fi

# Every applet /init calls. The provisioned busybox is built from a known config,
# but this check still earns its place: it guards the NODO_ALLOW_HOST_BUSYBOX dev
# path, where distros compile different applet sets. Symlinking blindly would
# defer the failure to guest boot ("applet not found"), where it looks like a nodo
# bug. `ip` is the sharp edge: without it the guest never configures its network
# from the ip= kernel argument.
APPLET_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/guest-kernel/applets.txt"
[ -f "$APPLET_FILE" ] || fail "Missing applet list at ${APPLET_FILE}"
mapfile -t BUSYBOX_APPLETS < <(grep -vE '^[[:space:]]*(#|$)' "$APPLET_FILE")
BUSYBOX_APPLET_LIST="$("$BUSYBOX_BIN" --list 2>/dev/null || true)"
if [ -z "$BUSYBOX_APPLET_LIST" ]; then
    fail "'$BUSYBOX_BIN --list' produced no output; cannot verify the applets /init needs."
fi
missing_applets=()
for applet in "${BUSYBOX_APPLETS[@]}"; do
    printf '%s\n' "$BUSYBOX_APPLET_LIST" | grep -qx "$applet" || missing_applets+=("$applet")
done
if [ "${#missing_applets[@]}" -gt 0 ]; then
    fail "busybox at ${BUSYBOX_BIN} lacks applets required by the guest init: ${missing_applets[*]}"
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
for applet in "${BUSYBOX_APPLETS[@]}"; do
    ln -sf /bin/busybox "$ROOT/bin/$applet"
done

cat > "$ROOT/init" <<'INIT_EOF'
#!/bin/sh
exec >/dev/console 2>&1
set -x
set -eu

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log() {
    echo "[nodo-ch-initramfs] $*" >&2
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

    log "detecting network interfaces..."
    log "available interfaces: $(ls /sys/class/net 2>/dev/null | tr '\n' ' ')"

    for p in /sys/class/net/*; do
        iface="${p##*/}"

        log "checking iface='$iface'"

        if [ "$iface" = "lo" ]; then
            log "skipping loopback"
            continue
        fi

        # Opcional: comprobar si tiene carrier (link up real)
        if [ -f "/sys/class/net/$iface/carrier" ]; then
            carrier="$(cat /sys/class/net/$iface/carrier 2>/dev/null || echo 0)"
            log "iface='$iface' carrier=$carrier"
        fi

        # Opcional: comprobar estado operativo
        if [ -f "/sys/class/net/$iface/operstate" ]; then
            state="$(cat /sys/class/net/$iface/operstate 2>/dev/null || echo unknown)"
            log "iface='$iface' operstate=$state"
        fi

        log "selected iface='$iface'"
        echo "$iface"
        return 0
    done

    log "no suitable non-loopback interface found"
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

# Mount the unified cgroup-v2 hierarchy. The guest has no init system (the service
# entrypoint runs as PID 1 straight out of switch_root), so nothing else mounts it.
# Container-runtime services (e.g. a service that boots its own dockerd) need it:
# without a cgroup mount, rootful dockerd defaults to legacy cgroup-v1, finds no
# controllers, and aborts with "Devices cgroup isn't mounted" -> PID 1 exits ->
# kernel panic. On v2 device control is eBPF, so no per-controller mounts are
# needed. Non-fatal: services that don't use cgroups are unaffected if it's absent.
mkdir -p /newroot/sys/fs/cgroup
mount -t cgroup2 none /newroot/sys/fs/cgroup \
    || log "warning: could not mount cgroup2 at /sys/fs/cgroup (container-runtime services may fail)"

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

# Shared filesystems (parent -> child inheritance). If the host injected a
# virtio-fs mount plan, mount each shared directory before switch_root. The plan
# is a JSON list of {"tag","path","ro"} objects; parse it with busybox tools
# (no jq in the initramfs). Absent plan => ordinary service, nothing to do.
if [ -f /newroot/.__nodo_virtiofs ]; then
    log "virtiofs: applying shared-filesystem mount plan"
    # Flatten each JSON object onto its own line (one {tag,path,ro} per line), then
    # iterate via redirect — NOT a pipe — so the loop runs in this shell and a
    # `fatal` actually halts init. Parsed with sed (no jq in the initramfs).
    tr '}' '\n' < /newroot/.__nodo_virtiofs > /tmp/.__nodo_virtiofs.lines
    while IFS= read -r obj; do
        case "$obj" in
            *'"tag"'*)
                vfs_tag=$(printf '%s' "$obj" | sed -n 's/.*"tag"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
                vfs_path=$(printf '%s' "$obj" | sed -n 's/.*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
                vfs_ro=$(printf '%s' "$obj" | sed -n 's/.*"ro"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p')
                [ -n "$vfs_tag" ] || continue
                case "$vfs_path" in
                    /*) ;;
                    *) fatal "virtiofs: guest path must be absolute, got '$vfs_path'" ;;
                esac
                mkdir -p "/newroot$vfs_path" || fatal "virtiofs: cannot create mountpoint $vfs_path"
                if [ "$vfs_ro" = "true" ]; then
                    mount -t virtiofs -o ro "$vfs_tag" "/newroot$vfs_path" \
                        || fatal "virtiofs: cannot mount $vfs_tag (ro) at $vfs_path"
                    log "virtiofs: mounted $vfs_tag -> $vfs_path (ro)"
                else
                    mount -t virtiofs "$vfs_tag" "/newroot$vfs_path" \
                        || fatal "virtiofs: cannot mount $vfs_tag at $vfs_path"
                    log "virtiofs: mounted $vfs_tag -> $vfs_path (rw)"
                fi
                ;;
        esac
    done < /tmp/.__nodo_virtiofs.lines
    rm -f /tmp/.__nodo_virtiofs.lines
fi

[ -x "/newroot$ENTRYPOINT" ] || fatal "entrypoint is not executable: $ENTRYPOINT"

log "switch_root -> $ENTRYPOINT"
exec switch_root /newroot "$ENTRYPOINT"
fatal "switch_root returned unexpectedly"
INIT_EOF
chmod 0755 "$ROOT/init"

printf 'nodo-ch-initramfs:v1\narch:%s\n' "$ARCH_TAG" > "$ROOT/etc/nodo-ch-initramfs.marker"

# Byte-reproducible output, so CI's published artifact can be checked against a
# local rebuild of the same commit — which is what makes the pinned digest in
# guest-kernel/SHA256SUMS.pinned auditable rather than just a checksum.
#
# The newc format records mode, mtime, uid, gid and inode numbers per entry, and
# the tree is staged in a fresh mktemp dir, so without all of these the same
# inputs produce a different file on every single run: the chmods pin the modes
# (`mkdir` and `printf >` inherit the caller's umask, so root's 022 and a
# developer's 077 archived different bytes), `touch` pins the mtimes, --owner
# pins ownership (CI runners are not root, installers are), --reproducible drops
# device/inode numbers, and `gzip -n` keeps the build timestamp out of the gzip
# header. `sort -z` already pinned the entry order.
find "$ROOT" -mindepth 1 -type d -exec chmod 0755 {} +
find "$ROOT" -mindepth 1 -type f -exec chmod 0644 {} +
chmod 0755 "$ROOT/init" "$ROOT/bin/busybox"
find "$ROOT" -mindepth 1 -exec touch -h -d @0 {} +

mkdir -p "$(dirname "$OUTPUT_PATH")"
(
    cd "$ROOT"
    find . -mindepth 1 -print0 \
        | sort -z \
        | cpio --null -o --format=newc --reproducible --owner 0:0 2>/dev/null \
        | gzip -9n > "$OUTPUT_PATH"
)
chmod 0644 "$OUTPUT_PATH"

# Verified with cpio, never lsinitramfs/lsinitrd. The gzip'd newc cpio layout is
# a kernel ABI, but every distro brands its own inspector for it (initramfs-tools
# ships lsinitramfs, dracut lsinitrd, mkinitcpio lsinitcpio), so gating this check
# on one of them skipped it silently everywhere else — including on the host that
# built the artifact. cpio is already a hard requirement above.
listing="$(gzip -dc "$OUTPUT_PATH" | cpio -t --quiet 2>/dev/null)"
for required in init bin/busybox etc/nodo-ch-initramfs.marker; do
    printf '%s\n' "$listing" | grep -qx "$required" \
        || fail "generated initramfs misses /${required}"
done

echo "Generated Cloud Hypervisor initramfs: $OUTPUT_PATH"
