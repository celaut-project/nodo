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
    git procps locales \
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

echo "5. Adding Deadsnakes PPA for Python 3.11..."
add-apt-repository ppa:deadsnakes/ppa -y >/dev/null

echo "6. Updating package lists after adding PPA..."
apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
    || { handle_apt_error $?; apt-get update; }

echo "7. Installing Python 3.11 and venv modules..."
apt-get install -y python3.11 python3.11-venv python3.11-distutils >/dev/null

echo "8. Installing pip for Python 3.11..."
wget -qO get-pip.py https://bootstrap.pypa.io/get-pip.py
python3.11 get-pip.py >/dev/null
rm get-pip.py

echo "9. Creating and activating virtualenv at $TARGET_DIR/venv..."
python3.11 -m venv "$TARGET_DIR/venv"
# shellcheck disable=SC1091
source "$TARGET_DIR/venv/bin/activate"

REQ_FILE="$TARGET_DIR/bash/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    echo "Error: requirements.txt not found at $REQ_FILE"
    deactivate
    exit 1
fi

echo "10. Installing Python dependencies..."
pip install --upgrade pip >/dev/null
if ! pip install -r "$REQ_FILE" >/dev/null; then
    echo "   - Failed to install Python packages."
    deactivate
    exit 1
fi

echo "11. Installing OpenJDK 21..."
apt-get install -y openjdk-21-jre-headless >/dev/null

echo "12. Downloading isolated Docker 24.0.9 binaries..."
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

echo "13. (Optional) Installing QEMU/binfmt for multi-architecture containers..."
apt-get install -y qemu-user-static binfmt-support >/dev/null
DOCKER_SOCKET="${TARGET_DIR}/docker/docker.sock"
/bin/bash "$TARGET_DIR/bash/start_docker_daemon.sh" "$TARGET_DIR" >/dev/null
"${TARGET_DIR}/bin/docker" -H "unix://${DOCKER_SOCKET}" run --rm --privileged multiarch/qemu-user-static --reset -p yes >/dev/null

echo "14. Installing Rust (cargo)…"
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
else
    echo "   - Rust already installed."
fi

echo "15. Running ARM-specific init script..."
if [ -f "$TARGET_DIR/bash/init_arm.sh" ]; then
    # Use 'source' so exported variables persist in this shell session
    source "$TARGET_DIR/bash/init_arm.sh"
else
    echo "   - init_arm.sh not found."
fi

echo "16. Running database migrations with Python..."
python3.11 nodo.py migrate >/dev/null || {
    echo "   - Migration failed."
    deactivate
    exit 1
}

echo "ARM setup completed successfully!"
deactivate
