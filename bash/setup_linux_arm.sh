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

    # Return curl/wget's own status: callers guard these with `|| fail`, and an
    # unconditional `return 0` turned every one of those guards into dead code.
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$destination" || return 1
        return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -qO "$destination" "$url" || return 1
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

PINNED_GUEST_SUMS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/guest-kernel/SHA256SUMS.pinned"

pinned_guest_digest() {
    # Expected digest for one guest asset, read from the in-tree pin.
    #
    # Deliberately NOT the SHA256SUMS published next to the artifact: that lives in
    # the same mutable release, so it proves the download was not truncated and
    # nothing else. Anyone able to edit the release replaces artifact and checksum
    # in one go. The pin is what makes GUEST_KERNEL_VERSION a content reference.
    local asset="$1"
    local pinned_tag digest

    [ -f "$PINNED_GUEST_SUMS" ] || fail "Missing pinned guest digests at ${PINNED_GUEST_SUMS}."

    pinned_tag="$(awk '$1 == "TAG" { print $2; exit }' "$PINNED_GUEST_SUMS")"
    [ -n "$pinned_tag" ] || fail "No TAG line in ${PINNED_GUEST_SUMS}."
    [ "$pinned_tag" = "$GUEST_KERNEL_VERSION" ] \
        || fail "Guest asset pin is for ${pinned_tag}, but this install wants ${GUEST_KERNEL_VERSION}. Update ${PINNED_GUEST_SUMS}."

    digest="$(awk -v name="$asset" '$2 == name { print $1; exit }' "$PINNED_GUEST_SUMS")"
    [ -n "$digest" ] || fail "No pinned digest for ${asset} in ${PINNED_GUEST_SUMS}."
    printf '%s\n' "$digest"
}

download_guest_asset() {
    # Guest artifacts come from the Nodo release, never from the host: a distro
    # kernel varies in format (Fedora/RHEL ship a CONFIG_EFI_ZBOOT PE that Cloud
    # Hypervisor cannot load at all) and a distro busybox varies in which applets
    # it was compiled with, so every node would run services on a different guest.
    # Both are built by .github/workflows/guest-kernel.yml from bash/guest-kernel/.
    local asset="$1"
    local destination="$2"
    local mode="$3"
    local base_url tmp_file expected actual

    base_url="https://github.com/celaut-project/nodo/releases/download/${GUEST_KERNEL_VERSION}"

    # `fail` inside a command substitution only exits the subshell, so the guard has
    # to be here: without it a bad pin left `expected` empty and the install limped
    # on to report a confusing checksum mismatch instead of the real problem.
    if ! expected="$(pinned_guest_digest "$asset")" || [ -z "$expected" ]; then
        fail "Refusing to install ${asset}: no usable pinned digest (see above)."
    fi

    tmp_file="$(mktemp /tmp/nodo-guest-asset.XXXXXX)"

    download_file "${base_url}/${asset}" "$tmp_file" \
        || fail "Unable to download ${asset} from release ${GUEST_KERNEL_VERSION}."

    actual="$(sha256sum "$tmp_file" | awk '{print $1}')"
    if [ "$expected" != "$actual" ]; then
        rm -f "$tmp_file"
        fail "SHA256 mismatch for ${asset}: expected ${expected} (pinned in ${PINNED_GUEST_SUMS}), got ${actual}. The release asset does not match this commit -- do not install it."
    fi

    install -m "$mode" "$tmp_file" "$destination"
    rm -f "$tmp_file"
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
    local ch_busybox_target="$TARGET_DIR/cloud_hypervisor/busybox/${CH_ARCH_TAG}/busybox"

    if [ ! -f "$CONFIG_FILE" ]; then
        fail "config.yaml not found at ${CONFIG_FILE}."
    fi
    if [ ! -x "$YQ_BIN" ]; then
        fail "Local yq binary is required at ${YQ_BIN}."
    fi

    mkdir -p "$(dirname "$ch_binary_target")"
    mkdir -p "$(dirname "$ch_kernel_target")"
    mkdir -p "$(dirname "$ch_initramfs_target")"
    mkdir -p "$(dirname "$ch_busybox_target")"

    echo "Provisioning Cloud Hypervisor assets..."
    if ! download_ch_binary "$ch_binary_target"; then
        fail "Unable to download Cloud Hypervisor ${CH_VERSION} release asset for arm64."
    fi

    # The whole guest comes from the release: kernel, initramfs, and the busybox
    # that is the initramfs' only binary. Building the initramfs here instead would
    # make the guest depend on the host's cpio, gzip and umask, which is the thing
    # a node must never vary by.
    echo "Provisioning guest kernel, initramfs and busybox ${GUEST_KERNEL_VERSION} for ${CH_ARCH_TAG}..."
    download_guest_asset "vmlinuz-${CH_ARCH_TAG//\//-}" "$ch_kernel_target" 0644
    download_guest_asset "initramfs-${CH_ARCH_TAG//\//-}" "$ch_initramfs_target" 0644
    # Kept on disk so an operator can rebuild the initramfs from this commit and
    # diff it against the shipped one; build_ch_initramfs.sh is byte-reproducible.
    download_guest_asset "busybox-${CH_ARCH_TAG//\//-}" "$ch_busybox_target" 0755

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
    test -x "$ch_busybox_target" || fail "Guest busybox download failed at ${ch_busybox_target}."
    test -f "$ch_initramfs_target" || fail "Guest initramfs download failed at ${ch_initramfs_target}."
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
