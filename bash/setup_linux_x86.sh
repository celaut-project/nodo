#!/bin/bash

set -euo pipefail

# Keep apt non-interactive: systemd/systemd-sysv (installed below for the WSL
# runtime) otherwise prompt to confirm replacing the init system.
export DEBIAN_FRONTEND=noninteractive

if [ -z "${1:-}" ]; then
  echo "Error: TARGET_DIR is not provided."
  exit 1
fi

TARGET_DIR="$1"
CH_VERSION="${2:-v51.1}"
GUEST_KERNEL_VERSION="${3:-guest-kernel-v1}"
CONFIG_FILE="$TARGET_DIR/config.yaml"

# Package names differ per distro; everything distro-specific lives here.
# shellcheck source=bash/lib_pkg.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_pkg.sh"
CH_ARCH_TAG="linux/amd64"

# Pinned portable runtimes.
PYTHON_VERSION="3.11.15"
PYTHON_BUILD_TAG="20260325"
PYTHON_ARCH="x86_64-unknown-linux-gnu"
PYTHON_DIST="cpython-${PYTHON_VERSION}+${PYTHON_BUILD_TAG}-${PYTHON_ARCH}-install_only_stripped.tar.gz"
PYTHON_BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_TAG}"
PYTHON_URL="${PYTHON_BASE_URL}/${PYTHON_DIST}"
PYTHON_CHECKSUMS_URL="${PYTHON_BASE_URL}/SHA256SUMS"

YQ_VERSION="v4.44.3"
YQ_URL="https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_amd64"
YQ_BIN_DEFAULT="$TARGET_DIR/bin/yq"
YQ_BIN="$YQ_BIN_DEFAULT"

RUNTIME_DIR="$TARGET_DIR/runtime"
PYTHON_RUNTIME_ROOT_DEFAULT="$RUNTIME_DIR/python"
PYTHON_RUNTIME_ROOT="$PYTHON_RUNTIME_ROOT_DEFAULT"
JAVA_RUNTIME_ROOT_DEFAULT="$RUNTIME_DIR/java"

fail() {
    echo "Error: $1"
    exit 1
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

apply_configured_dependency_paths() {
    local configured_yq_bin

    configured_yq_bin="$(read_config_path_or_default '.dependencies.yq.BIN' "$YQ_BIN_DEFAULT")"
    if [ "$configured_yq_bin" != "$YQ_BIN" ]; then
        mkdir -p "$(dirname "$configured_yq_bin")"
        cp -f "$YQ_BIN" "$configured_yq_bin"
        chmod +x "$configured_yq_bin"
        YQ_BIN="$configured_yq_bin"
    fi

    PYTHON_RUNTIME_ROOT="$(read_config_path_or_default '.dependencies.python.RUNTIME_ROOT' "$PYTHON_RUNTIME_ROOT_DEFAULT")"
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

extract_archive() {
    local archive="$1"
    local destination="$2"
    local tmp_dir

    tmp_dir="$(mktemp -d)"
    tar -xzf "$archive" -C "$tmp_dir"

    shopt -s nullglob
    local entries=("$tmp_dir"/*)
    shopt -u nullglob

    rm -rf "$destination"
    mkdir -p "$destination"

    if [ "${#entries[@]}" -eq 1 ] && [ -d "${entries[0]}" ]; then
        cp -a "${entries[0]}/." "$destination/"
    else
        cp -a "$tmp_dir/." "$destination/"
    fi

    rm -rf "$tmp_dir"
}

verify_archive_sha256_from_checksums() {
    local archive_path="$1"
    local checksums_path="$2"
    local artifact_name="$3"

    local expected
    local actual

    expected="$(awk -v name="$artifact_name" '{
        file=$2
        gsub(/^\*/, "", file)
        if (file == name || $NF == name) {
            print $1
            exit
        }
    }' "$checksums_path")"
    if [ -z "$expected" ]; then
        fail "Unable to find SHA256 for ${artifact_name} in ${checksums_path}."
    fi

    actual="$(sha256sum "$archive_path" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
        fail "SHA256 mismatch for ${artifact_name}. expected=${expected} actual=${actual}"
    fi
}

download_guest_kernel() {
    # The guest kernel is a Nodo release asset, not the host's /boot kernel: a
    # distro kernel varies in format (Fedora/RHEL ship a CONFIG_EFI_ZBOOT PE that
    # Cloud Hypervisor cannot load at all), in size, and in what it enables, so
    # every node would boot services on a different kernel. Built by
    # .github/workflows/guest-kernel.yml from bash/guest-kernel/.
    local destination="$1"
    local asset base_url tmp_file sums_file expected actual

    # "linux/amd64" -> "vmlinuz-linux-amd64"
    asset="vmlinuz-${CH_ARCH_TAG//\//-}"
    base_url="https://github.com/celaut-project/nodo/releases/download/${GUEST_KERNEL_VERSION}"

    tmp_file="$(mktemp /tmp/nodo-guest-kernel.XXXXXX)"
    sums_file="$(mktemp /tmp/nodo-guest-kernel-sha.XXXXXX)"

    download_file "${base_url}/${asset}" "$tmp_file" \
        || fail "Unable to download guest kernel ${asset} from release ${GUEST_KERNEL_VERSION}."
    download_file "${base_url}/SHA256SUMS" "$sums_file" \
        || fail "Unable to download SHA256SUMS from release ${GUEST_KERNEL_VERSION}."

    expected="$(awk -v name="$asset" '{f=$2; gsub(/^\*/,"",f); if (f==name) {print $1; exit}}' "$sums_file")"
    [ -n "$expected" ] || fail "No SHA256 entry for ${asset} in ${GUEST_KERNEL_VERSION} SHA256SUMS."
    actual="$(sha256sum "$tmp_file" | awk '{print $1}')"
    [ "$expected" = "$actual" ] \
        || fail "Guest kernel SHA256 mismatch for ${asset}: expected ${expected}, got ${actual}."

    install -m 0644 "$tmp_file" "$destination"
    rm -f "$tmp_file" "$sums_file"
}

download_ch_binary() {
    local destination="$1"
    local base_url="https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/${CH_VERSION}"
    local tmp_file
    local assets=(
        "cloud-hypervisor-static"
        "cloud-hypervisor-static-x86_64"
    )

    tmp_file="$(mktemp /tmp/cloud-hypervisor.XXXXXX)"
    for asset in "${assets[@]}"; do
        if download_file "${base_url}/${asset}" "$tmp_file"; then
            install -m 0755 "$tmp_file" "$destination"
            rm -f "$tmp_file"
            return 0
        fi
    done

    rm -f "$tmp_file"
    return 1
}

install_local_yq() {
    mkdir -p "$(dirname "$YQ_BIN_DEFAULT")"
    download_file "$YQ_URL" "$YQ_BIN_DEFAULT"
    chmod +x "$YQ_BIN_DEFAULT"
    YQ_BIN="$YQ_BIN_DEFAULT"
    test -x "$YQ_BIN" || fail "yq local binary is not executable at ${YQ_BIN}"
}

install_portable_python() {
    local archive
    local checksums
    local install_dir

    archive="$(mktemp /tmp/nodo-python.XXXXXX.tar.gz)"
    checksums="$(mktemp /tmp/nodo-python-sha.XXXXXX)"
    install_dir="${PYTHON_RUNTIME_ROOT}/${PYTHON_VERSION}+${PYTHON_BUILD_TAG}"

    mkdir -p "$PYTHON_RUNTIME_ROOT"

    echo "Installing portable Python ${PYTHON_VERSION} (${PYTHON_BUILD_TAG})..."
    download_file "$PYTHON_URL" "$archive"
    download_file "$PYTHON_CHECKSUMS_URL" "$checksums"

    verify_archive_sha256_from_checksums "$archive" "$checksums" "$PYTHON_DIST"
    extract_archive "$archive" "$install_dir"

    ln -sfn "$install_dir" "${PYTHON_RUNTIME_ROOT}/current"
    test -x "${PYTHON_RUNTIME_ROOT}/current/bin/python3" \
        || fail "Portable Python not found at ${PYTHON_RUNTIME_ROOT}/current/bin/python3"

    rm -f "$archive" "$checksums"
}

provision_cloud_hypervisor_assets() {
    local ch_binary_target="$TARGET_DIR/bin/cloud-hypervisor"
    local ch_kernel_target="$TARGET_DIR/cloud_hypervisor/kernels/${CH_ARCH_TAG}/vmlinuz"
    local ch_initramfs_target="$TARGET_DIR/cloud_hypervisor/initramfs/${CH_ARCH_TAG}/initramfs"
    local ch_initramfs_builder="$TARGET_DIR/bash/build_ch_initramfs.sh"

    if [ ! -f "$CONFIG_FILE" ]; then
        fail "config.yaml not found at ${CONFIG_FILE}."
    fi
    if [ ! -x "$YQ_BIN" ]; then
        fail "Local yq binary is required at ${YQ_BIN}."
    fi
    if [ ! -x "$ch_initramfs_builder" ]; then
        fail "Missing executable initramfs builder at ${ch_initramfs_builder}."
    fi

    mkdir -p "$(dirname "$ch_binary_target")"
    mkdir -p "$(dirname "$ch_kernel_target")"
    mkdir -p "$(dirname "$ch_initramfs_target")"

    echo "Downloading Cloud Hypervisor ${CH_VERSION} static binary..."
    if ! download_ch_binary "$ch_binary_target"; then
        fail "Unable to download Cloud Hypervisor ${CH_VERSION} release asset for x86_64."
    fi

    echo "Provisioning guest kernel ${GUEST_KERNEL_VERSION} for ${CH_ARCH_TAG}..."
    download_guest_kernel "$ch_kernel_target"

    "$ch_initramfs_builder" "$TARGET_DIR" "$CH_ARCH_TAG" "$ch_initramfs_target"

    CH_BINARY_TARGET="$ch_binary_target" "$YQ_BIN" -i \
        '.virtualizers.ch.BINARY_PATH = strenv(CH_BINARY_TARGET)' \
        "$CONFIG_FILE"
    CH_ARCH_TAG="$CH_ARCH_TAG" CH_KERNEL_TARGET="$ch_kernel_target" "$YQ_BIN" -i \
        '.virtualizers.ch.KERNEL_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_KERNEL_TARGET)' \
        "$CONFIG_FILE"
    CH_ARCH_TAG="$CH_ARCH_TAG" CH_INITRAMFS_TARGET="$ch_initramfs_target" "$YQ_BIN" -i \
        '.virtualizers.ch.INITRAMFS_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_INITRAMFS_TARGET)' \
        "$CONFIG_FILE"

    test -x "$ch_binary_target" || fail "Cloud Hypervisor binary is not executable at ${ch_binary_target}."
    test -f "$ch_kernel_target" || fail "Guest kernel download failed at ${ch_kernel_target}."
    test -f "$ch_initramfs_target" || fail "Initramfs copy failed at ${ch_initramfs_target}."
}

echo "Detecting package manager..."
detect_pkg_mgr

echo "Updating package lists..."
pkg_update

pkg_install_host_dependencies

# The WSL image is a Debian rootfs exported by docs/RELEASING.md: it needs an
# init system of its own so `nodo daemon` and nodo.service work once it boots.
if [ "$PKG_MGR" = "apt" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd systemd-sysv \
        || fail "Failed to install systemd."
fi

ensure_utf8_locale
verify_host_tools

echo "Installing local yq runtime..."
install_local_yq
apply_configured_dependency_paths

echo "Restricting executable architectures to this host (x86_64)..."
"$YQ_BIN" -i '.builder.ARM_SUPPORT = false | .packer.ARM_PACKER_SUPPORT = false' "$CONFIG_FILE"

echo "Provisioning Cloud Hypervisor assets..."
provision_cloud_hypervisor_assets

install_portable_python

echo "Creating Python virtual environment in ${TARGET_DIR}/venv..."
"${PYTHON_RUNTIME_ROOT}/current/bin/python3" -m venv "$TARGET_DIR/venv"

REQ_FILE="$TARGET_DIR/bash/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    fail "requirements.txt not found at ${REQ_FILE}"
fi

if ! command -v clang >/dev/null 2>&1; then
    # The portable CPython records CC=clang in sysconfig; without it, source
    # builds (psutil on aarch64) fail with "No such file or directory: clang".
    export CC="${CC:-gcc}" CXX="${CXX:-g++}"
fi

"$TARGET_DIR/venv/bin/python" -m pip install --upgrade pip > /dev/null
"$TARGET_DIR/venv/bin/python" -m pip install -r "$REQ_FILE" > /dev/null

# No Docker install: nodo runs services under Cloud Hypervisor and delegates
# packing to the external packer-service. Docker is never installed on this host.

echo "Running migrations with local Python runtime..."
"$TARGET_DIR/venv/bin/python" "$TARGET_DIR/nodo.py" migrate > /dev/null

configure_systemd_service() {
    # The release rootfs is imported and run directly (the end user does not
    # re-run install.sh), so the systemd unit and WSL systemd-boot config must be
    # baked in here — otherwise `nodo daemon ...` fails with `systemctl: not
    # found` on a fresh import. Render the same unit install.sh does.
    local unit_src="$TARGET_DIR/bash/nodo.service.template"
    local unit_dst="/etc/systemd/system/nodo.service"
    [ -f "$unit_src" ] || fail "nodo.service.template not found at $unit_src"

    echo "Rendering systemd unit ${unit_dst}..."
    sed \
        -e "s|{{MAIN_DIR}}|${TARGET_DIR}|g" \
        -e "s|{{JAVA_HOME}}|${JAVA_RUNTIME_ROOT_DEFAULT}/current|g" \
        -e "s|{{PYTHON_RUNTIME_BIN_DIR}}|${PYTHON_RUNTIME_ROOT}/current/bin|g" \
        -e "s|{{PYTHON_VENV_BIN}}|python|g" \
        "$unit_src" > "$unit_dst"
    chmod 644 "$unit_dst"
    if grep -q '{{[A-Z_][A-Z_]*}}' "$unit_dst"; then
        fail "Unresolved placeholders remain in ${unit_dst}."
    fi

    # Enable the unit. systemd is not PID1 in this build container, so
    # `systemctl enable` is unavailable — create the wants symlink by hand.
    mkdir -p /etc/systemd/system/multi-user.target.wants
    ln -sf "$unit_dst" /etc/systemd/system/multi-user.target.wants/nodo.service

    # Boot systemd as PID1 under WSL (so `systemctl` works) and default the distro
    # to root — nodo needs root for Cloud Hypervisor networking/microVMs.
    echo "Writing /etc/wsl.conf (systemd boot + default root user)..."
    cat > /etc/wsl.conf <<'WSLCONF'
[boot]
systemd=true

[user]
default=root
WSLCONF
}

echo "Configuring systemd service and WSL boot..."
configure_systemd_service

echo "All steps completed."
