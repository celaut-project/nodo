#!/bin/bash
# Private directory for the node
NODO_DIR="${1:-$HOME/.nodo}"
DOCKER_DIR="$NODO_DIR/docker"
BIN_DIR="$DOCKER_DIR/bin"
DATA_DIR="$DOCKER_DIR/data"
RUN_DIR="$DOCKER_DIR/run"

echo "Configuring Docker Rootless in $DOCKER_DIR..."

mkdir -p "$BIN_DIR" "$DATA_DIR" "$RUN_DIR"

# 1. Install necessary dependencies if missing
# Note: This might require sudo once, or the user may already have them.
# In an ideal environment, the user already has slirp4netns and uidmap.
if ! command -v newuidmap >/dev/null 2>&1 || ! command -v slirp4netns >/dev/null 2>&1; then
    echo "Installing dependencies (slirp4netns, uidmap)..."
    sudo apt-get update && sudo apt-get install -y slirp4netns uidmap
fi

# 2. Download static Docker binaries (if they don't exist)
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    DOCKER_ARCH="x86_64"
elif [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    DOCKER_ARCH="aarch64"
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi

DOCKER_VERSION="24.0.7"
if [ ! -f "$BIN_DIR/dockerd" ]; then
    echo "Downloading Docker $DOCKER_VERSION binaries for $DOCKER_ARCH..."
    curl -L "https://download.docker.com/linux/static/stable/$DOCKER_ARCH/docker-$DOCKER_VERSION.tgz" | tar -xz -C "$BIN_DIR" --strip-components=1
fi

# 3. Download Rootless extras (RootlessKit, etc.)
if [ ! -f "$BIN_DIR/rootlesskit" ]; then
    echo "Downloading Docker Rootless extras for $DOCKER_ARCH..."
    curl -L "https://download.docker.com/linux/static/stable/$DOCKER_ARCH/docker-rootless-extras-$DOCKER_VERSION.tgz" | tar -xz -C "$BIN_DIR" --strip-components=1
fi

# 4. Configure subuid and subgid if not already configured

if ! grep -q "$USER" /etc/subuid; then
    echo "Configuring subuid/subgid for $USER..."
    sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
fi

echo "Docker Rootless installed successfully in $BIN_DIR"
