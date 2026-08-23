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
# The arch this host cannot run under KVM, and therefore the one QEMU emulates.
# Its guest assets are installed too (see provision_guest_assets_for_arch).
FOREIGN_ARCH_TAG="linux/arm64"

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

provision_guest_assets_for_arch() {
    # Guest kernel + initramfs + busybox for ONE architecture, and the config
    # entries that point at them.
    #
    # Called for the host arch AND for the foreign one. The foreign assets are what
    # QEMU boots under emulation, and src/utils/architectures.py decides whether to
    # advertise that arch by looking for exactly these files -- so an install that
    # shipped only the host's arch could never execute the other one, no matter what
    # the config said. That was the bug: config.yaml listed an arm64 kernel path on
    # x86_64 hosts that nothing ever downloaded.
    local arch_tag="$1"
    local asset_suffix="${arch_tag//\//-}"
    local kernel_target="$TARGET_DIR/cloud_hypervisor/kernels/${arch_tag}/vmlinuz"
    local initramfs_target="$TARGET_DIR/cloud_hypervisor/initramfs/${arch_tag}/initramfs"
    local busybox_target="$TARGET_DIR/cloud_hypervisor/busybox/${arch_tag}/busybox"

    mkdir -p "$(dirname "$kernel_target")"
    mkdir -p "$(dirname "$initramfs_target")"
    mkdir -p "$(dirname "$busybox_target")"

    # The whole guest comes from the release: kernel, initramfs, and the busybox
    # that is the initramfs' only binary. Building the initramfs here instead would
    # make the guest depend on the host's cpio, gzip and umask, which is the thing
    # a node must never vary by.
    echo "Provisioning guest kernel, initramfs and busybox ${GUEST_KERNEL_VERSION} for ${arch_tag}..."
    download_guest_asset "vmlinuz-${asset_suffix}" "$kernel_target" 0644
    download_guest_asset "initramfs-${asset_suffix}" "$initramfs_target" 0644
    # Kept on disk so an operator can rebuild the initramfs from this commit and
    # diff it against the shipped one; build_ch_initramfs.sh is byte-reproducible.
    download_guest_asset "busybox-${asset_suffix}" "$busybox_target" 0755

    ARCH_TAG="$arch_tag" KERNEL_TARGET="$kernel_target" "$YQ_BIN" -i \
        '.virtualizers.ch.KERNEL_PATHS[strenv(ARCH_TAG)] = strenv(KERNEL_TARGET)' \
        "$CONFIG_FILE"
    ARCH_TAG="$arch_tag" INITRAMFS_TARGET="$initramfs_target" "$YQ_BIN" -i \
        '.virtualizers.ch.INITRAMFS_PATHS[strenv(ARCH_TAG)] = strenv(INITRAMFS_TARGET)' \
        "$CONFIG_FILE"

    test -f "$kernel_target" || fail "Guest kernel download failed at ${kernel_target}."
    test -f "$initramfs_target" || fail "Guest initramfs download failed at ${initramfs_target}."
    test -x "$busybox_target" || fail "Guest busybox download failed at ${busybox_target}."
}

provision_cloud_hypervisor_assets() {
    local ch_binary_target="$TARGET_DIR/bin/cloud-hypervisor"

    if [ ! -f "$CONFIG_FILE" ]; then
        fail "config.yaml not found at ${CONFIG_FILE}."
    fi
    if [ ! -x "$YQ_BIN" ]; then
        fail "Local yq binary is required at ${YQ_BIN}."
    fi

    mkdir -p "$(dirname "$ch_binary_target")"

    echo "Downloading Cloud Hypervisor ${CH_VERSION} static binary..."
    if ! download_ch_binary "$ch_binary_target"; then
        fail "Unable to download Cloud Hypervisor ${CH_VERSION} release asset for x86_64."
    fi

    CH_BINARY_TARGET="$ch_binary_target" "$YQ_BIN" -i \
        '.virtualizers.ch.BINARY_PATH = strenv(CH_BINARY_TARGET)' \
        "$CONFIG_FILE"

    test -x "$ch_binary_target" || fail "Cloud Hypervisor binary is not executable at ${ch_binary_target}."

    # Both architectures: the host's, which CH boots under KVM, and the foreign
    # one, which QEMU boots under TCG.
    provision_guest_assets_for_arch "$CH_ARCH_TAG"
    provision_guest_assets_for_arch "$FOREIGN_ARCH_TAG"
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

# Only the PACKER is restricted to this host: a local build runs the target's own
# toolchain, and nodo installs no binfmt handler, so packing arm64 here cannot work
# (src/utils/arch_guard.py). EXECUTION is not restricted by config at all -- the
# node derives it from the host arch plus what QEMU can emulate, so the arm64
# assets and emulator installed below are what make arm64 executable here.
echo "Restricting the packer to this host's architecture (x86_64)..."
"$YQ_BIN" -i '.packer.ARM_PACKER_SUPPORT = false' "$CONFIG_FILE"

install_foreign_arch_emulator "$FOREIGN_ARCH_TAG"

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
    local unit_tmp
    [ -f "$unit_src" ] || fail "nodo.service.template not found at $unit_src"

    echo "Rendering systemd unit ${unit_dst}..."
    # Render to a temporary file and only publish it once every placeholder is
    # resolved. Writing straight to unit_dst left an unloadable unit installed
    # ("Group={{ADMIN_GROUP}}") when the check below tripped, so a failed install
    # broke the service the previous install had left working.
    unit_tmp="$(mktemp)"
    sed \
        -e "s|{{MAIN_DIR}}|${TARGET_DIR}|g" \
        -e "s|{{JAVA_HOME}}|${JAVA_RUNTIME_ROOT_DEFAULT}/current|g" \
        -e "s|{{PYTHON_RUNTIME_BIN_DIR}}|${PYTHON_RUNTIME_ROOT}/current/bin|g" \
        -e "s|{{PYTHON_VENV_BIN}}|python|g" \
        -e "s|{{ADMIN_GROUP}}|$(resolve_admin_group)|g" \
        "$unit_src" > "$unit_tmp"
    if grep -q '{{[A-Z_][A-Z_]*}}' "$unit_tmp"; then
        local unresolved
        unresolved="$(grep -o '{{[A-Z_][A-Z_]*}}' "$unit_tmp" | sort -u | tr '\n' ' ')"
        rm -f "$unit_tmp"
        fail "Unresolved placeholders in rendered unit: ${unresolved}(${unit_dst} left untouched)."
    fi
    install -m 0644 "$unit_tmp" "$unit_dst"
    rm -f "$unit_tmp"

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
