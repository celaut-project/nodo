#!/bin/bash

set -e

if [ -z "$1" ]; then
  echo "Error: TARGET_DIR is not provided."
  exit 1
fi

TARGET_DIR="$1"
CH_VERSION="${2:-v51.1}"
CONFIG_FILE="$TARGET_DIR/config.yaml"
CH_ARCH_TAG="linux/amd64"

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
        "cloud-hypervisor-static"
        "cloud-hypervisor-static-x86_64"
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

    echo "Downloading Cloud Hypervisor ${CH_VERSION} static binary..."
    if ! download_ch_binary "$ch_binary_target"; then
        fail "Unable to download Cloud Hypervisor ${CH_VERSION} release asset for x86_64."
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

handle_update_errors() {
    exit_code=$1
    echo "Failed to update package lists. Exit code: $exit_code"

    case $exit_code in
        100)
            echo "Lock file exists, maybe another package manager is running. Attempting to remove lock file and retrying..."
            sudo rm /var/lib/apt/lists/lock
            ;;
        200)
            echo "Authentication error. Verify if GPG keys are properly added."
            ;;
        *)
            echo "Unknown error occurred during package update."
            ;;
    esac
}

echo "Updating package lists..."
sudo apt-get -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false update > /dev/null 2>&1 || {
    handle_update_errors $?
}

echo "Installing required build dependencies..."
if sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev protobuf-compiler \
                           libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev \
                           busybox-static cpio gzip initramfs-tools-core iputils-ping > /dev/null 2>&1; then
    echo "Dependencies installed successfully."
else
    echo "Error installing dependencies. Attempting to fix broken dependencies..."
    if sudo apt --fix-broken install -y > /dev/null 2>&1; then
        echo "Fixed broken dependencies. Retrying to install required build dependencies..."
        if sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
                                   libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev \
                                   busybox-static cpio gzip initramfs-tools-core iputils-ping > /dev/null 2>&1; then
            echo "Dependencies installed successfully after fixing broken dependencies."
        else
            echo "Failed to install dependencies after fixing broken dependencies. Please check manually."
            exit 1
        fi
    else
        echo "Failed to fix broken dependencies. Please check manually."
        exit 1
    fi
fi

echo "Installing yq for YAML processing..."
if ! command -v yq &> /dev/null; then
    sudo wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq
    sudo chmod +x /usr/local/bin/yq
fi

echo "Provisioning Cloud Hypervisor assets..."
provision_cloud_hypervisor_assets

echo "Adding Python 3.11 repository..."
sudo add-apt-repository ppa:deadsnakes/ppa -y > /dev/null

echo "Updating package lists after adding Python repository..."
sudo apt-get -y update > /dev/null 2>&1 || {
    handle_update_errors $?
}

echo "Installing Python 3.11 and pip..."
sudo apt-get -y install python3.11 python3.11-venv python3.11-distutils > /dev/null

echo "Installing pip for Python 3.11..."
wget -q https://bootstrap.pypa.io/get-pip.py -O get-pip.py
sudo python3.11 get-pip.py > /dev/null
rm get-pip.py

echo "Creating and activating Python virtual environment..."
python3.11 -m venv "$TARGET_DIR/venv"
source "$TARGET_DIR/venv/bin/activate"

REQUIREMENTS_FILE="$TARGET_DIR/bash/requirements.txt"

# Check if requirements.txt exists
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "Error: requirements.txt not found at $REQUIREMENTS_FILE"
    deactivate
    exit 1
fi

echo "Installing Python dependencies from $REQUIREMENTS_FILE..."
if ! python3 -m pip install -r "$REQUIREMENTS_FILE" > /dev/null; then
    echo "Error: Failed to install Python packages from requirements.txt."
    deactivate
    exit 1
fi

echo "Installing OpenJDK 21"
sudo apt-get -y install openjdk-21-jre-headless

echo "Installing required system packages for Docker ..."
sudo apt-get -y install ca-certificates curl gnupg lsb-release > /dev/null

# Docker installation
echo "Downloading isolated Docker 24.0.9 binaries..."
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
# End of Docker installation

echo "Installing QEMU and binfmt-support for multi-architecture support..."
sudo apt-get -y install qemu-system binfmt-support qemu-user-static > /dev/null

# Configure QEMU for multi-architecture support using nodo's isolated Docker daemon
DOCKER_SOCKET="${TARGET_DIR}/docker/docker.sock"
/bin/bash "$TARGET_DIR/bash/start_docker_daemon.sh" "$TARGET_DIR" > /dev/null
"${TARGET_DIR}/bin/docker" -H "unix://${DOCKER_SOCKET}" run --rm --privileged multiarch/qemu-user-static --reset -p yes > /dev/null

echo "Executing initialization script for x86..."
# Use 'source' so exported variables persist in this shell session
source "$TARGET_DIR/bash/init_x86.sh"

echo "Running migrations for Python application..."
python3.11 "$TARGET_DIR/nodo.py" migrate > /dev/null

echo "All steps completed."
