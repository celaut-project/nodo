#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run this script as root or with sudo."
    exit 1
fi

TARGET_DIR="${1:-}"
if [ -z "$TARGET_DIR" ]; then
    echo "Error: You must pass the project root directory as the first argument."
    exit 1
fi

CH_VERSION="${2:-v51.1}"
GUEST_KERNEL_VERSION="${3:-guest-kernel-v1}"
CONFIG_FILE="$TARGET_DIR/config.yaml"

# Package names differ per distro; everything distro-specific lives here.
# shellcheck source=bash/lib_pkg.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_pkg.sh"
CH_ARCH_TAG="linux/arm64"

# Pinned portable runtimes.
PYTHON_VERSION="3.11.15"
PYTHON_BUILD_TAG="20260325"
PYTHON_ARCH="aarch64-unknown-linux-gnu"
PYTHON_DIST="cpython-${PYTHON_VERSION}+${PYTHON_BUILD_TAG}-${PYTHON_ARCH}-install_only_stripped.tar.gz"
PYTHON_BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_TAG}"
PYTHON_URL="${PYTHON_BASE_URL}/${PYTHON_DIST}"
PYTHON_CHECKSUMS_URL="${PYTHON_BASE_URL}/SHA256SUMS"

YQ_VERSION="v4.44.3"
YQ_URL="https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_arm64"
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

    # "linux/arm64" -> "vmlinuz-linux-arm64"
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
        "cloud-hypervisor-static-aarch64"
        "cloud-hypervisor-static-arm64"
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

    echo "Provisioning Cloud Hypervisor assets..."
    if ! download_ch_binary "$ch_binary_target"; then
        fail "Unable to download Cloud Hypervisor ${CH_VERSION} release asset for arm64."
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
ensure_utf8_locale
verify_host_tools

echo "Installing local yq runtime..."
install_local_yq
apply_configured_dependency_paths

echo "Restricting executable architectures to this host (arm64)..."
"$YQ_BIN" -i '.builder.X86_SUPPORT = false | .packer.X86_PACKER_SUPPORT = false' "$CONFIG_FILE"

echo "Provisioning Cloud Hypervisor..."
provision_cloud_hypervisor_assets

install_portable_python

echo "Creating and preparing Python virtualenv..."
"${PYTHON_RUNTIME_ROOT}/current/bin/python3" -m venv "$TARGET_DIR/venv"

REQ_FILE="$TARGET_DIR/bash/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    fail "requirements.txt not found at $REQ_FILE"
fi

if ! command -v clang >/dev/null 2>&1; then
    # The portable CPython records CC=clang in sysconfig; without it, source
    # builds (psutil on aarch64) fail with "No such file or directory: clang".
    export CC="${CC:-gcc}" CXX="${CXX:-g++}"
fi

"$TARGET_DIR/venv/bin/python" -m pip install --upgrade pip >/dev/null
if ! "$TARGET_DIR/venv/bin/python" -m pip install -r "$REQ_FILE" >/dev/null; then
    fail "Failed to install Python packages."
fi

# No Docker install: nodo runs services under Cloud Hypervisor and delegates
# packing to the external packer-service. Docker is never installed on this host.

echo "Installing Rust (cargo)..."
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
fi

echo "Running Python database migrations..."
"$TARGET_DIR/venv/bin/python" "$TARGET_DIR/nodo.py" migrate >/dev/null || {
    fail "Migration failed."
}

echo "ARM setup completed successfully!"
