#!/bin/bash

set -e

if [ -z "$1" ]; then
  echo "Error: TARGET_DIR is not provided."
  exit 1
fi

TARGET_DIR="$1"

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
                           libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev > /dev/null 2>&1; then
    echo "Dependencies installed successfully."
else
    echo "Error installing dependencies. Attempting to fix broken dependencies..."
    if sudo apt --fix-broken install -y > /dev/null 2>&1; then
        echo "Fixed broken dependencies. Retrying to install required build dependencies..."
        if sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
                                   libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev > /dev/null 2>&1; then
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
if [ "$ARCH" = "x86_64" ]; then ARCH="amd64"; fi
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then ARCH="arm64"; fi

DOCKER_TGZ="docker-24.0.9.tgz"
curl -fsSL "https://download.docker.com/linux/static/stable/${ARCH}/${DOCKER_TGZ}" -o "/tmp/${DOCKER_TGZ}"
tar -xzf "/tmp/${DOCKER_TGZ}" -C "/tmp/"
cp "/tmp/docker/docker" "$BIN_DIR/"
cp "/tmp/docker/dockerd" "$BIN_DIR/"
cp /tmp/docker/containerd* "$BIN_DIR/" 2>/dev/null || true
cp /tmp/docker/ctr "$BIN_DIR/" 2>/dev/null || true
cp "/tmp/docker/runc" "$BIN_DIR/" 2>/dev/null || true
rm -rf "/tmp/docker" "/tmp/${DOCKER_TGZ}"
chmod +x "$BIN_DIR"/*

echo "Downloading isolated buildx v0.12.1 plugin..."
BUILDX_URL="https://github.com/docker/buildx/releases/download/v0.12.1/buildx-v0.12.1.linux-${ARCH}"
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
