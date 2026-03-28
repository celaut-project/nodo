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

    echo "Provisioning Cloud Hypervisor assets..."
    if ! download_ch_binary "$ch_binary_target"; then
        fail "Unable to download Cloud Hypervisor ${CH_VERSION} release asset for arm64."
    fi

    kernel_source="$(resolve_boot_asset "/boot/vmlinuz" "vmlinuz-*")" \
        || fail "Unable to locate kernel in /boot (checked /boot/vmlinuz and vmlinuz-*)."

    cp -f "$kernel_source" "$ch_kernel_target"
    chmod 0644 "$ch_kernel_target"

    "$ch_initramfs_builder" "$TARGET_DIR" "$CH_ARCH_TAG" "$ch_initramfs_target"

    CH_BINARY_TARGET="$ch_binary_target" yq -i \
        '.virtualizers.ch.BINARY_PATH = strenv(CH_BINARY_TARGET)' \
        "$CONFIG_FILE"
    CH_ARCH_TAG="$CH_ARCH_TAG" CH_KERNEL_TARGET="$ch_kernel_target" yq -i \
        '.virtualizers.ch.KERNEL_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_KERNEL_TARGET)' \
        "$CONFIG_FILE"
    CH_ARCH_TAG="$CH_ARCH_TAG" CH_INITRAMFS_TARGET="$ch_initramfs_target" yq -i \
        '.virtualizers.ch.INITRAMFS_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_INITRAMFS_TARGET)' \
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

echo "Updating package lists..."
apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
    || { handle_apt_error $?; apt-get update; }

echo "Installing build dependencies and basic tools..."
apt-get install -y --no-install-recommends \
    build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
    libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev \
    ca-certificates curl gnupg lsb-release software-properties-common \
    git procps locales busybox-static cpio gzip initramfs-tools-core iputils-ping \
    > /dev/null || { handle_apt_error $?; exit 1; }

echo "Ensuring UTF-8 locale support..."
locale-gen en_US.UTF-8 >/dev/null || true
update-locale LANG=en_US.UTF-8

echo "Installing yq..."
if ! command -v yq >/dev/null; then
    wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_arm64
    chmod +x /usr/local/bin/yq
fi

echo "Provisioning Cloud Hypervisor..."
provision_cloud_hypervisor_assets

# --- Función de instalación segura de binarios ---
install_tmp() {
    local src="$1"
    local dst="$2"
    local tmp="${dst}.new.$$"
    cp "$src" "$tmp"
    chmod +x "$tmp"
    mv -f "$tmp" "$dst"
}

echo "Adding Deadsnakes PPA for Python 3.11..."
add-apt-repository ppa:deadsnakes/ppa -y >/dev/null
apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
    || { handle_apt_error $?; apt-get update; }

echo "Installing Python 3.11 and venv modules..."
apt-get install -y python3.11 python3.11-venv python3.11-distutils >/dev/null

echo "Installing pip for Python 3.11..."
wget -qO get-pip.py https://bootstrap.pypa.io/get-pip.py
python3.11 get-pip.py >/dev/null
rm get-pip.py

echo "Creating and activating Python virtualenv..."
python3.11 -m venv "$TARGET_DIR/venv"
source "$TARGET_DIR/venv/bin/activate"

REQ_FILE="$TARGET_DIR/bash/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    echo "Error: requirements.txt not found at $REQ_FILE"
    deactivate
    exit 1
fi

echo "Installing Python dependencies..."
pip install --upgrade pip >/dev/null
if ! pip install -r "$REQ_FILE" >/dev/null; then
    echo "Failed to install Python packages."
    deactivate
    exit 1
fi

echo "Installing OpenJDK 21..."
apt-get install -y openjdk-21-jre-headless >/dev/null

echo "Downloading isolated Docker 24.0.9 binaries..."
BIN_DIR="${TARGET_DIR}/bin"
PLUGIN_DIR="${TARGET_DIR}/libexec/docker/cli-plugins"
mkdir -p "$BIN_DIR" "$PLUGIN_DIR"

# Detener dockerd si está corriendo
pkill -f "${TARGET_DIR}/bin/dockerd" 2>/dev/null || true

ARCH=$(uname -m)
DOCKER_ARCH="$ARCH"
BUILDX_ARCH="$ARCH"
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

install_tmp "/tmp/docker/docker" "$BIN_DIR/docker"
install_tmp "/tmp/docker/dockerd" "$BIN_DIR/dockerd"

cp /tmp/docker/containerd* "$BIN_DIR/" 2>/dev/null || true
cp /tmp/docker/ctr "$BIN_DIR/" 2>/dev/null || true
cp /tmp/docker/runc "$BIN_DIR/" 2>/dev/null || true

rm -rf "/tmp/docker" "/tmp/${DOCKER_TGZ}"
chmod +x "$BIN_DIR"/*

echo "Downloading buildx plugin..."
curl -fsSL "https://github.com/docker/buildx/releases/download/v0.12.1/buildx-v0.12.1.linux-${BUILDX_ARCH}" \
    -o "${PLUGIN_DIR}/docker-buildx"
chmod +x "${PLUGIN_DIR}/docker-buildx"

echo "Installing QEMU/binfmt for multi-architecture containers..."
apt-get install -y qemu-user-static binfmt-support >/dev/null
DOCKER_SOCKET="${TARGET_DIR}/docker/docker.sock"
/bin/bash "$TARGET_DIR/bash/start_docker_daemon.sh" "$TARGET_DIR" >/dev/null
"${TARGET_DIR}/bin/docker" -H "unix://${DOCKER_SOCKET}" run --rm --privileged multiarch/qemu-user-static --reset -p yes >/dev/null

echo "Installing Rust (cargo)…"
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
fi

echo "Running Python database migrations..."
python3.11 "$TARGET_DIR/nodo.py" migrate >/dev/null || {
    echo "Migration failed."
    deactivate
    exit 1
}

echo "ARM setup completed successfully!"
deactivate