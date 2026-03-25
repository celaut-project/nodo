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
CONFIG_FILE="$TARGET_DIR/config.yaml"
CH_ARCH_TAG="linux/arm64"

fail() {
    echo "Error: $1"
    exit 1
}

resolve_boot_asset() {
    local preferred_path="$1"
    local fallback_pattern="$2"
    local resolved_path=""

    if [ -L "$preferred_path" ] || [ -f "$preferred_path" ]; then
        resolved_path="$(readlink -f "$preferred_path")"
        if [ -n "$resolved_path" ] && [ -f "$resolved_path" ]; then
            printf '%s\n' "$resolved_path"
            return 0
        fi
    fi

    resolved_path="$(
        find /boot -maxdepth 1 -type f -name "$fallback_pattern" -printf '%T@ %p\n' \
            | sort -nr \
            | head -n1 \
            | cut -d' ' -f2-
    )"
    if [ -n "$resolved_path" ] && [ -f "$resolved_path" ]; then
        printf '%s\n' "$resolved_path"
        return 0
    fi

    return 1
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
        if wget -qO "$tmp_file" "${base_url}/${asset}"; then
            install -m 0755 "$tmp_file" "$destination"
            rm -f "$tmp_file"
            return 0
        fi
    done

    rm -f "$tmp_file"
    return 1
}

provision_cloud_hypervisor_assets() {
    local ch_binary_target="$TARGET_DIR/bin/cloud-hypervisor"
    local ch_kernel_target="$TARGET_DIR/cloud_hypervisor/kernels/${CH_ARCH_TAG}/vmlinuz"
    local ch_initramfs_target="$TARGET_DIR/cloud_hypervisor/initramfs/${CH_ARCH_TAG}/initramfs"
    local ch_initramfs_builder="$TARGET_DIR/bash/build_ch_initramfs.sh"
    local kernel_source

    if [ ! -f "$CONFIG_FILE" ]; then
        fail "config.yaml not found at ${CONFIG_FILE}."
    fi
    if ! command -v yq >/dev/null 2>&1; then
        fail "yq is required to update ${CONFIG_FILE}."
    fi
    if [ ! -x "$ch_initramfs_builder" ]; then
        fail "Missing executable initramfs builder at ${ch_initramfs_builder}."
    fi

    mkdir -p "$(dirname "$ch_binary_target")"
    mkdir -p "$(dirname "$ch_kernel_target")"
    mkdir -p "$(dirname "$ch_initramfs_target")"

    echo "5. Provisioning Cloud Hypervisor assets..."
    if ! download_ch_binary "$ch_binary_target"; then
        fail "Unable to download Cloud Hypervisor ${CH_VERSION} release asset for arm64."
    fi

    kernel_source="$(resolve_boot_asset "/boot/vmlinuz" "vmlinuz-*")" \
        || fail "Unable to locate kernel in /boot (checked /boot/vmlinuz and vmlinuz-*)."

    cp -f "$kernel_source" "$ch_kernel_target"
    chmod 0644 "$ch_kernel_target"

    "$ch_initramfs_builder" "$TARGET_DIR" "$CH_ARCH_TAG" "$ch_initramfs_target"

    CH_BINARY_TARGET="$ch_binary_target" yq -i \
        '.virtualizers.cloud_hypervisor.BINARY_PATH = strenv(CH_BINARY_TARGET)' \
        "$CONFIG_FILE"
    CH_ARCH_TAG="$CH_ARCH_TAG" CH_KERNEL_TARGET="$ch_kernel_target" yq -i \
        '.virtualizers.cloud_hypervisor.KERNEL_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_KERNEL_TARGET)' \
        "$CONFIG_FILE"
    CH_ARCH_TAG="$CH_ARCH_TAG" CH_INITRAMFS_TARGET="$ch_initramfs_target" yq -i \
        '.virtualizers.cloud_hypervisor.INITRAMFS_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_INITRAMFS_TARGET)' \
        "$CONFIG_FILE"

    test -x "$ch_binary_target" || fail "Cloud Hypervisor binary is not executable at ${ch_binary_target}."
    test -f "$ch_kernel_target" || fail "Kernel copy failed at ${ch_kernel_target}."
    test -f "$ch_initramfs_target" || fail "Initramfs copy failed at ${ch_initramfs_target}."
}

handle_apt_error() {
    local code=$1
    echo "apt-get error (code $code)."
    case "$code" in
        100)
            echo "  - Lock file issue. Removing locks and retrying..."
            rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock*
            dpkg --configure -a
            ;;
        200)
            echo "  - GPG authentication error. Check your keys."
            ;;
        *)
            echo "  - Unknown APT error."
            ;;
    esac
}

echo "1. Updating package lists..."
apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
    || { handle_apt_error $?; apt-get update; }

echo "2. Installing build dependencies and basic tools..."
apt-get install -y --no-install-recommends \
    build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
    libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev \
    ca-certificates curl gnupg lsb-release software-properties-common \
    git procps locales busybox-static cpio gzip initramfs-tools-core iputils-ping \
    > /dev/null || { handle_apt_error $?; exit 1; }

echo "3. Ensuring UTF-8 locale support..."
locale-gen en_US.UTF-8 >/dev/null || true
update-locale LANG=en_US.UTF-8

echo "4. Installing yq for YAML parsing..."
if ! command -v yq >/dev/null; then
    wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_arm64
    chmod +x /usr/local/bin/yq
    echo "   - yq installed."
else
    echo "   - yq already installed."
fi

validate_cloud_hypervisor_kvm() {
    local ch_binary="$TARGET_DIR/bin/cloud-hypervisor"
    local ch_kernel="$TARGET_DIR/cloud_hypervisor/kernels/${CH_ARCH_TAG}/vmlinuz"
    local ch_initramfs="$TARGET_DIR/cloud_hypervisor/initramfs/${CH_ARCH_TAG}/initramfs"

    echo "Running Cloud Hypervisor KVM compatibility smoke test..."

    if [ ! -x "$ch_binary" ]; then
        echo "Warning: CH binary not found, skipping smoke test."
        return 0
    fi
    if [ ! -f "$ch_kernel" ]; then
        echo "Warning: Guest kernel not found, skipping smoke test."
        return 0
    fi
    if [ ! -f "$ch_initramfs" ]; then
        echo "Warning: Initramfs not found, skipping smoke test."
        return 0
    fi
    if [ ! -e /dev/kvm ]; then
        echo "Warning: /dev/kvm not available, skipping smoke test."
        return 0
    fi

    local smoke_dir
    smoke_dir="$(mktemp -d /tmp/nodo-ch-smoke.XXXXXX)"

    local rootfs="$smoke_dir/rootfs.ext4"
    local api_sock="$smoke_dir/ch.sock"
    local stderr_log="$smoke_dir/ch.stderr.log"
    local serial_log="$smoke_dir/ch.serial.log"

    # Create a minimal ext4 image
    dd if=/dev/zero of="$rootfs" bs=1M count=16 > /dev/null 2>&1 || {
        echo "Warning: Could not create test rootfs, skipping smoke test."
        rm -rf "$smoke_dir"
        return 0
    }
    mkfs.ext4 -F -q "$rootfs" > /dev/null 2>&1 || {
        echo "Warning: Could not format test rootfs, skipping smoke test."
        rm -rf "$smoke_dir"
        return 0
    }

    "$ch_binary" \
        --api-socket "$api_sock" \
        --kernel "$ch_kernel" \
        --initramfs "$ch_initramfs" \
        --disk "path=$rootfs,image_type=raw" \
        --cpus boot=1 \
        --memory size=64M \
        --cmdline "root=/dev/vda rw console=ttyS0" \
        --serial "file=$serial_log" \
        --console off \
        > /dev/null 2> "$stderr_log" &
    local ch_pid=$!

    sleep 3

    if kill -0 "$ch_pid" 2>/dev/null; then
        # VM is running — vCPU works on this kernel
        echo "Cloud Hypervisor KVM smoke test passed."
        kill "$ch_pid" 2>/dev/null
        wait "$ch_pid" 2>/dev/null
        rm -rf "$smoke_dir"
        return 0
    fi

    # Process exited early — check why
    local stderr_content=""
    [ -f "$stderr_log" ] && stderr_content="$(cat "$stderr_log" 2>/dev/null)"

    rm -rf "$smoke_dir"

    if echo "$stderr_content" | grep -qE "VcpuRun|InternalError"; then
        local kernel_release
        kernel_release="$(uname -r)"
        echo ""
        echo "============================================================"
        echo "FATAL: Cloud Hypervisor vCPU failed on this host."
        echo ""
        echo "The Cloud Hypervisor binary (${CH_VERSION}) is incompatible"
        echo "with the host kernel (${kernel_release})."
        echo ""
        echo "stderr: ${stderr_content}"
        echo ""
        echo "Solutions:"
        echo "  1. Upgrade Cloud Hypervisor to a newer version."
        echo "  2. Downgrade the host kernel to a stable release"
        echo "     (e.g. 6.8, 6.11, or 6.12 LTS)."
        echo "============================================================"
        echo ""
        fail "Cloud Hypervisor is incompatible with host kernel ${kernel_release}. See details above."
    fi

    if echo "$stderr_content" | grep -q "KernelLoad"; then
        echo ""
        echo "============================================================"
        echo "FATAL: Cloud Hypervisor could not load the guest kernel."
        echo ""
        echo "The vmlinuz at ${ch_kernel} may be incompatible or corrupt."
        echo "stderr: ${stderr_content}"
        echo "============================================================"
        echo ""
        fail "Guest kernel is not loadable by Cloud Hypervisor. Re-provision kernel assets."
    fi

    echo "Warning: Cloud Hypervisor exited early during smoke test."
    echo "  stderr: ${stderr_content}"
    echo "  Nodo may not be able to run services with Cloud Hypervisor on this host."
    return 0
}

provision_cloud_hypervisor_assets

echo "Validating Cloud Hypervisor on this host..."
validate_cloud_hypervisor_kvm

echo "6. Adding Deadsnakes PPA for Python 3.11..."
add-apt-repository ppa:deadsnakes/ppa -y >/dev/null

echo "7. Updating package lists after adding PPA..."
apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
    || { handle_apt_error $?; apt-get update; }

echo "8. Installing Python 3.11 and venv modules..."
apt-get install -y python3.11 python3.11-venv python3.11-distutils >/dev/null

echo "9. Installing pip for Python 3.11..."
wget -qO get-pip.py https://bootstrap.pypa.io/get-pip.py
python3.11 get-pip.py >/dev/null
rm get-pip.py

echo "10. Creating and activating virtualenv at $TARGET_DIR/venv..."
python3.11 -m venv "$TARGET_DIR/venv"
# shellcheck disable=SC1091
source "$TARGET_DIR/venv/bin/activate"

REQ_FILE="$TARGET_DIR/bash/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    echo "Error: requirements.txt not found at $REQ_FILE"
    deactivate
    exit 1
fi

echo "11. Installing Python dependencies..."
pip install --upgrade pip >/dev/null
if ! pip install -r "$REQ_FILE" >/dev/null; then
    echo "   - Failed to install Python packages."
    deactivate
    exit 1
fi

echo "12. Installing OpenJDK 21..."
apt-get install -y openjdk-21-jre-headless >/dev/null

echo "13. Downloading isolated Docker 24.0.9 binaries..."
NODO_DIR="$TARGET_DIR"
BIN_DIR="${NODO_DIR}/bin"
PLUGIN_DIR="${NODO_DIR}/libexec/docker/cli-plugins"
mkdir -p "$BIN_DIR" "$PLUGIN_DIR"

ARCH=$(uname -m)
DOCKER_ARCH="$ARCH"
BUILDX_ARCH="$ARCH"
# Docker static binaries use x86_64/aarch64, buildx uses amd64/arm64
if [ "$ARCH" = "x86_64" ] || [ "$ARCH" = "amd64" ]; then
    DOCKER_ARCH="x86_64"
    BUILDX_ARCH="amd64"
fi
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    DOCKER_ARCH="aarch64"
    BUILDX_ARCH="arm64"
fi

DOCKER_TGZ="docker-24.0.9.tgz"
curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/${DOCKER_TGZ}" -o "/tmp/${DOCKER_TGZ}"
tar -xzf "/tmp/${DOCKER_TGZ}" -C "/tmp/"
cp "/tmp/docker/docker" "$BIN_DIR/"
cp "/tmp/docker/dockerd" "$BIN_DIR/"
cp /tmp/docker/containerd* "$BIN_DIR/" 2>/dev/null || true
cp /tmp/docker/ctr "$BIN_DIR/" 2>/dev/null || true
cp "/tmp/docker/runc" "$BIN_DIR/" 2>/dev/null || true
rm -rf "/tmp/docker" "/tmp/${DOCKER_TGZ}"
chmod +x "$BIN_DIR"/*

echo "Downloading isolated buildx v0.12.1 plugin..."
BUILDX_URL="https://github.com/docker/buildx/releases/download/v0.12.1/buildx-v0.12.1.linux-${BUILDX_ARCH}"
curl -fsSL "$BUILDX_URL" -o "${PLUGIN_DIR}/docker-buildx"
chmod +x "${PLUGIN_DIR}/docker-buildx"

echo "14. (Optional) Installing QEMU/binfmt for multi-architecture containers..."
apt-get install -y qemu-user-static binfmt-support >/dev/null
DOCKER_SOCKET="${TARGET_DIR}/docker/docker.sock"
/bin/bash "$TARGET_DIR/bash/start_docker_daemon.sh" "$TARGET_DIR" >/dev/null
"${TARGET_DIR}/bin/docker" -H "unix://${DOCKER_SOCKET}" run --rm --privileged multiarch/qemu-user-static --reset -p yes >/dev/null

echo "15. Installing Rust (cargo)…"
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
else
    echo "   - Rust already installed."
fi

echo "16. Running database migrations with Python..."
python3.11 nodo.py migrate >/dev/null || {
    echo "   - Migration failed."
    deactivate
    exit 1
}

echo "ARM setup completed successfully!"
deactivate
