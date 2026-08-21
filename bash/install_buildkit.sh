#!/bin/bash
# install_buildkit.sh
# Installs the ROOTLESS builder toolchain used by nodo's optional local packer
# (`packer.local: true`): BuildKit (buildkitd + buildctl + buildkit-runc) under
# MAIN_DIR, plus the one-time host prerequisites a rootless user namespace needs.
#
# The local packer never needed Docker: its only use of it was
# `docker buildx build --output type=tar`, and buildx is a front end for BuildKit.
# Driving BuildKit directly lets the builder run as the invoking user, which is
# what removes sudo from `nodo pack` entirely — a daemon we own is a daemon we
# can always stop (a root dockerd could not be signalled by an unprivileged
# pack, so a failed build used to leave it wedged and block every later pack).
#
# This script is the ONLY place that may need sudo, and only the first time:
# installing uidmap/rootlesskit and allocating subuid/subgid ranges are host-wide
# operations. Everything already in place is skipped, so a provisioned host runs
# this without any privileged call at all.
#
# Usage: bash install_buildkit.sh <MAIN_DIR>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/lib_rootless.sh
. "${SCRIPT_DIR}/lib_rootless.sh"

TARGET_DIR="${1:-}"
if [ -z "$TARGET_DIR" ]; then
    echo "Error: You must pass the project root directory as the first argument."
    exit 1
fi

TARGET_DIR="$(cd "$TARGET_DIR" >/dev/null 2>&1 && pwd)"
CONFIG_FILE="$TARGET_DIR/config.yaml"

# Pinned, well-known upstream releases. Override via the environment if needed.
BUILDKIT_VERSION="${NODO_BUILDKIT_VERSION:-0.32.2}"
ROOTLESSKIT_VERSION="${NODO_ROOTLESSKIT_VERSION:-3.1.0}"

case "$(uname -m)" in
    x86_64|amd64)
        BUILDKIT_ARCH="linux-amd64"
        ROOTLESSKIT_ARCH="x86_64"
        ;;
    aarch64|arm64)
        BUILDKIT_ARCH="linux-arm64"
        ROOTLESSKIT_ARCH="aarch64"
        ;;
    *)
        echo "Error: Unsupported architecture $(uname -m)."
        exit 1
        ;;
esac

BUILDKIT_URL="https://github.com/moby/buildkit/releases/download/v${BUILDKIT_VERSION}/buildkit-v${BUILDKIT_VERSION}.${BUILDKIT_ARCH}.tar.gz"
ROOTLESSKIT_URL="https://github.com/rootless-containers/rootlesskit/releases/download/v${ROOTLESSKIT_VERSION}/rootlesskit-${ROOTLESSKIT_ARCH}.tar.gz"

BIN_DIR_DEFAULT="$TARGET_DIR/bin"
YQ_BIN="$TARGET_DIR/bin/yq"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

fail() {
    echo "Error: $1"
    exit 1
}

download_file() {
    local url="$1"
    local destination="$2"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$destination"
        return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -qO "$destination" "$url"
        return 0
    fi
    fail "Neither curl nor wget is available to download ${url}"
}

expand_main_dir_placeholder() {
    printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"
}

read_config_path_or_default() {
    local query="$1"
    local default_value="$2"
    local value=""

    if [ -x "$YQ_BIN" ] && [ -f "$CONFIG_FILE" ]; then
        value="$("$YQ_BIN" -r "$query // \"\"" "$CONFIG_FILE" 2>/dev/null || true)"
    fi

    if [ -z "$value" ] || [ "$value" = "null" ]; then
        value="$default_value"
    fi

    expand_main_dir_placeholder "$value"
}

verify_sha256() {
    # verify_sha256 <file> <expected>  — enforced only when <expected> is non-empty.
    local file="$1"
    local expected="$2"
    [ -n "$expected" ] || return 0
    local actual
    actual="$(sha256sum "$file" | awk '{print $1}')"
    [ "$actual" = "$expected" ] || fail "SHA256 mismatch for ${file}. expected=${expected} actual=${actual}"
}

# --- One-time host prerequisites ---------------------------------------------
# Package names for the same logical dependency differ per distro; anything the
# distro does not carry falls back to a static binary further down.
pkg_install() {
    local logical="$1"
    local pkg=""

    if command -v apt-get >/dev/null 2>&1; then
        case "$logical" in
            uidmap) pkg="uidmap" ;;
            rootlesskit) pkg="rootlesskit" ;;
        esac
        [ -n "$pkg" ] || return 1
        ${SUDO} apt-get update -qq || true
        ${SUDO} apt-get install -y "$pkg"
        return $?
    fi
    if command -v dnf >/dev/null 2>&1; then
        case "$logical" in
            uidmap) pkg="shadow-utils" ;;
            rootlesskit) pkg="rootlesskit" ;;
        esac
        [ -n "$pkg" ] || return 1
        ${SUDO} dnf install -y "$pkg"
        return $?
    fi
    if command -v zypper >/dev/null 2>&1; then
        case "$logical" in
            uidmap) pkg="shadow" ;;
            rootlesskit) pkg="rootlesskit" ;;
        esac
        [ -n "$pkg" ] || return 1
        ${SUDO} zypper --non-interactive install "$pkg"
        return $?
    fi
    if command -v pacman >/dev/null 2>&1; then
        case "$logical" in
            uidmap) pkg="shadow" ;;
            *) return 1 ;;  # rootlesskit is AUR-only on Arch; use the static binary
        esac
        ${SUDO} pacman -S --noconfirm "$pkg"
        return $?
    fi
    if command -v apk >/dev/null 2>&1; then
        case "$logical" in
            uidmap) pkg="shadow-uidmap" ;;
            *) return 1 ;;
        esac
        ${SUDO} apk add "$pkg"
        return $?
    fi
    return 1
}

install_static_rootlesskit() {
    # Fallback for distros without a rootlesskit package. On AppArmor distros a
    # binary outside /usr/bin is NOT covered by the shipped
    # /etc/apparmor.d/rootlesskit profile, so a matching profile is installed for
    # this path — otherwise the kernel denies it the user namespace.
    local bin_dir="$1"
    local archive
    archive="$(mktemp /tmp/nodo-rootlesskit.XXXXXX.tgz)"
    local extract_dir
    extract_dir="$(mktemp -d /tmp/nodo-rootlesskit.XXXXXX)"

    echo "Downloading ${ROOTLESSKIT_URL} ..."
    download_file "$ROOTLESSKIT_URL" "$archive"
    verify_sha256 "$archive" "${NODO_ROOTLESSKIT_SHA256:-}"
    tar -xzf "$archive" -C "$extract_dir"
    for b in rootlesskit rootlesskit-docker-proxy; do
        [ -f "$extract_dir/$b" ] && cp -f "$extract_dir/$b" "$bin_dir/$b" && chmod +x "$bin_dir/$b"
    done
    rm -rf "$archive" "$extract_dir"
    test -x "$bin_dir/rootlesskit" || fail "rootlesskit not installed at $bin_dir/rootlesskit"

    if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ]; then
        local profile="/etc/apparmor.d/nodo-rootlesskit"
        echo "AppArmor restricts unprivileged user namespaces; installing a profile for ${bin_dir}/rootlesskit ..."
        ${SUDO} tee "$profile" >/dev/null <<PROFILE
# Installed by nodo (bash/install_buildkit.sh). Mirrors Ubuntu's shipped
# /etc/apparmor.d/rootlesskit profile, which only grants the userns exemption to
# /usr/bin/rootlesskit — nodo's copy lives elsewhere and needs its own entry.
abi <abi/4.0>,
include <tunables/global>

profile nodo-rootlesskit ${bin_dir}/rootlesskit flags=(unconfined) {
  userns,

  include if exists <local/nodo-rootlesskit>
}
PROFILE
        ${SUDO} apparmor_parser -r "$profile" 2>/dev/null \
            || echo "Warning: could not load ${profile}; run 'sudo apparmor_parser -r ${profile}' manually."
    fi
}

# --- Retire any leftover root dockerd from the previous toolchain -------------
# The old isolated daemon ran as root via sudo, so an unprivileged pack could not
# signal it at all: a failed build left it alive holding its data-root lock, and
# every later pack died with "already running but is not responding". Those
# orphans outlive the switch to the rootless builder, and this installer is the
# one place that has the privileges to clear them.
cleanup_legacy_dockerd() {
    local data_root="${TARGET_DIR}/docker/data"
    local pids
    pids="$(pgrep -af dockerd 2>/dev/null \
        | grep -F -- "--data-root=${data_root}" \
        | grep -vE 'sudo( |$)' \
        | awk '{print $1}' || true)"
    [ -n "$pids" ] || return 0

    echo "Found a leftover root dockerd from the retired Docker toolchain (PID(s): ${pids}). Stopping it..."
    for p in $pids; do ${SUDO} kill "$p" 2>/dev/null || true; done
    sleep 3
    pids="$(pgrep -af dockerd 2>/dev/null \
        | grep -F -- "--data-root=${data_root}" \
        | grep -vE 'sudo( |$)' \
        | awk '{print $1}' || true)"
    if [ -n "$pids" ]; then
        for p in $pids; do ${SUDO} kill -9 "$p" 2>/dev/null || true; done
        sleep 2
    fi
    echo "Leftover dockerd stopped. ${TARGET_DIR}/docker and ${TARGET_DIR}/bin/docker* are now unused;"
    echo "remove them at your convenience to reclaim the space."
}

ensure_rootless_prereqs() {
    local bin_dir="$1"
    local missing
    missing="$(rootless_prereqs_missing || true)"
    if [ -z "$missing" ]; then
        echo "Rootless prerequisites already satisfied; nothing privileged to do."
        return 0
    fi

    echo "Provisioning rootless prerequisites (this is the only step that needs sudo,"
    echo "and only this once — a pack itself never asks for privileges):"
    echo "  missing: $(echo ${missing} | tr '\n' ' ')"

    if echo "$missing" | grep -qE '^(newuidmap|newgidmap)$'; then
        echo "Installing uidmap (provides newuidmap/newgidmap) ..."
        pkg_install uidmap || fail "Could not install the uidmap package. Install it with your package manager and re-run."
    fi

    if echo "$missing" | grep -qx "rootlesskit"; then
        echo "Installing rootlesskit ..."
        if ! pkg_install rootlesskit; then
            echo "No rootlesskit package for this distro; falling back to the upstream static binary."
            install_static_rootlesskit "$bin_dir"
        fi
    fi

    # Subordinate id ranges: without them the namespace maps a single uid and
    # most Dockerfiles fail on `apt-get install`/`chown`.
    local user
    user="$(id -un)"
    if echo "$missing" | grep -qx "subuid"; then
        echo "Allocating a subordinate uid range for ${user} ..."
        ${SUDO} usermod --add-subuids 100000-165535 "$user" \
            || fail "Could not allocate subuids for ${user}. Add '${user}:100000:65536' to /etc/subuid and re-run."
    fi
    if echo "$missing" | grep -qx "subgid"; then
        echo "Allocating a subordinate gid range for ${user} ..."
        ${SUDO} usermod --add-subgids 100000-165535 "$user" \
            || fail "Could not allocate subgids for ${user}. Add '${user}:100000:65536' to /etc/subgid and re-run."
    fi

    missing="$(rootless_prereqs_missing || true)"
    [ -z "$missing" ] || fail "Rootless prerequisites still missing after provisioning: $(echo ${missing} | tr '\n' ' ')"
}

# --- Resolve install locations (config overrides fall back to defaults) -------
BUILDCTL_BIN="$(read_config_path_or_default '.dependencies.buildkit.BIN' "$BIN_DIR_DEFAULT/buildctl")"
BUILDKITD_BIN="$(read_config_path_or_default '.dependencies.buildkit.DAEMON_BIN' "$BIN_DIR_DEFAULT/buildkitd")"

BIN_DIR="$(dirname "$BUILDCTL_BIN")"
mkdir -p "$BIN_DIR"

echo "Installing rootless BuildKit ${BUILDKIT_VERSION} for nodo..."
echo "  Binaries: ${BIN_DIR}"

# --- BuildKit static bundle ---------------------------------------------------
archive="$(mktemp /tmp/nodo-buildkit.XXXXXX.tgz)"
extract_dir="$(mktemp -d /tmp/nodo-buildkit.XXXXXX)"
trap 'rm -rf "$archive" "$extract_dir"' EXIT

echo "Downloading ${BUILDKIT_URL} ..."
download_file "$BUILDKIT_URL" "$archive"
verify_sha256 "$archive" "${NODO_BUILDKIT_SHA256:-}"

tar -xzf "$archive" -C "$extract_dir"
SRC_DIR="$extract_dir/bin"
[ -d "$SRC_DIR" ] || fail "Unexpected BuildKit bundle layout; bin/ dir not found in ${BUILDKIT_URL}"

# buildkit-qemu-* is deliberately skipped: the packer only ever builds for the
# native architecture (src/utils/arch_guard.py), so ~55MB of emulators would be
# dead weight. The CNI helpers are kept — they are small and cover the non-host
# worker network modes.
for b in buildkitd buildctl buildkit-runc \
         buildkit-cni-bridge buildkit-cni-firewall buildkit-cni-host-local buildkit-cni-loopback; do
    if [ -f "$SRC_DIR/$b" ]; then
        cp -f "$SRC_DIR/$b" "$BIN_DIR/$b"
        chmod +x "$BIN_DIR/$b"
    fi
done

test -x "$BUILDKITD_BIN" || fail "buildkitd not installed at ${BUILDKITD_BIN}"
test -x "$BUILDCTL_BIN" || fail "buildctl not installed at ${BUILDCTL_BIN}"

# --- Host prerequisites + verification ---------------------------------------
cleanup_legacy_dockerd
ensure_rootless_prereqs "$BIN_DIR"

ROOTLESSKIT_BIN="$(resolve_rootlesskit "$BIN_DIR")"
echo "Verifying the user namespace actually works (${ROOTLESSKIT_BIN}) ..."
if ! "$ROOTLESSKIT_BIN" --net=host true 2>/tmp/nodo-rootlesskit-probe.log; then
    echo "----- rootlesskit output -----"
    cat /tmp/nodo-rootlesskit-probe.log || true
    echo "------------------------------"
    rm -f /tmp/nodo-rootlesskit-probe.log
    fail "rootlesskit could not create a user namespace. See docs/ROOTLESS.md."
fi
rm -f /tmp/nodo-rootlesskit-probe.log

echo "Rootless BuildKit installed:"
echo "  buildkitd   -> ${BUILDKITD_BIN}"
echo "  buildctl    -> ${BUILDCTL_BIN}"
echo "  rootlesskit -> ${ROOTLESSKIT_BIN}"
echo "nodo starts and stops this builder around each local pack — as your own user, no sudo."
