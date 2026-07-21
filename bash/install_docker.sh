#!/bin/bash
# install_docker.sh
# Installs an ISOLATED, node-local Docker toolchain (dockerd + docker client +
# buildx) for nodo's optional local packer (`packer.local: true`).
#
# Like install_java.sh, this is an on-demand, portable install: the binaries land
# under MAIN_DIR (bin/ + libexec/docker/cli-plugins/) and nodo drives them against
# a private socket/data-root (see start_docker_daemon.sh). It is installed FOR THE
# NODE ONLY and is independent of any Docker already present on the host — nodo
# never touches the system Docker daemon, socket, or data.
#
# Usage: bash install_docker.sh <MAIN_DIR>

set -euo pipefail

TARGET_DIR="${1:-}"
if [ -z "$TARGET_DIR" ]; then
    echo "Error: You must pass the project root directory as the first argument."
    exit 1
fi

TARGET_DIR="$(cd "$TARGET_DIR" >/dev/null 2>&1 && pwd)"
CONFIG_FILE="$TARGET_DIR/config.yaml"

# Pinned, well-known upstream releases. Override via the environment if needed.
DOCKER_VERSION="${NODO_DOCKER_VERSION:-27.3.1}"
BUILDX_VERSION="${NODO_BUILDX_VERSION:-0.18.0}"

case "$(uname -m)" in
    x86_64|amd64)
        DOCKER_ARCH="x86_64"
        BUILDX_ARCH="linux-amd64"
        ;;
    aarch64|arm64)
        DOCKER_ARCH="aarch64"
        BUILDX_ARCH="linux-arm64"
        ;;
    *)
        echo "Error: Unsupported architecture $(uname -m)."
        exit 1
        ;;
esac

DOCKER_URL="https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_VERSION}.tgz"
BUILDX_URL="https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.${BUILDX_ARCH}"

BIN_DIR_DEFAULT="$TARGET_DIR/bin"
PLUGIN_DIR_DEFAULT="$TARGET_DIR/libexec/docker/cli-plugins"
YQ_BIN_DEFAULT="$TARGET_DIR/bin/yq"
YQ_BIN="$YQ_BIN_DEFAULT"

fail() {
    echo "Error: $1"
    exit 1
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

verify_sha256() {
    # verify_sha256 <file> <expected>  — enforced only when <expected> is non-empty.
    local file="$1"
    local expected="$2"
    [ -n "$expected" ] || return 0
    local actual
    actual="$(sha256sum "$file" | awk '{print $1}')"
    [ "$actual" = "$expected" ] || fail "SHA256 mismatch for ${file}. expected=${expected} actual=${actual}"
}

# Resolve install locations (config overrides fall back to portable defaults).
DOCKER_BIN="$(read_config_path_or_default '.dependencies.docker.BIN' "$BIN_DIR_DEFAULT/docker")"
DOCKERD_BIN="$(read_config_path_or_default '.dependencies.docker.DAEMON_BIN' "$BIN_DIR_DEFAULT/dockerd")"
BUILDX_BIN="$(read_config_path_or_default '.dependencies.docker.BUILDX_BIN' "$PLUGIN_DIR_DEFAULT/docker-buildx")"

BIN_DIR="$(dirname "$DOCKER_BIN")"
PLUGIN_DIR="$(dirname "$BUILDX_BIN")"

mkdir -p "$BIN_DIR" "$PLUGIN_DIR"

echo "Installing isolated Docker ${DOCKER_VERSION} + buildx ${BUILDX_VERSION} for nodo..."
echo "  Binaries: ${BIN_DIR}"
echo "  Buildx plugin: ${PLUGIN_DIR}"

# --- Docker static bundle (dockerd, docker, containerd, runc, ...) ------------
archive="$(mktemp /tmp/nodo-docker.XXXXXX.tgz)"
extract_dir="$(mktemp -d /tmp/nodo-docker.XXXXXX)"
trap 'rm -rf "$archive" "$extract_dir"' EXIT

echo "Downloading ${DOCKER_URL} ..."
download_file "$DOCKER_URL" "$archive"
verify_sha256 "$archive" "${NODO_DOCKER_SHA256:-}"

tar -xzf "$archive" -C "$extract_dir"
# The bundle extracts to a top-level docker/ directory.
SRC_DIR="$extract_dir/docker"
[ -d "$SRC_DIR" ] || fail "Unexpected Docker bundle layout; docker/ dir not found in ${DOCKER_URL}"

for b in dockerd docker containerd containerd-shim-runc-v2 ctr runc docker-init docker-proxy; do
    if [ -f "$SRC_DIR/$b" ]; then
        cp -f "$SRC_DIR/$b" "$BIN_DIR/$b"
        chmod +x "$BIN_DIR/$b"
    fi
done

test -x "$DOCKERD_BIN" || fail "dockerd not installed at ${DOCKERD_BIN}"
test -x "$DOCKER_BIN" || fail "docker client not installed at ${DOCKER_BIN}"

# --- Buildx CLI plugin --------------------------------------------------------
echo "Downloading ${BUILDX_URL} ..."
download_file "$BUILDX_URL" "$BUILDX_BIN"
verify_sha256 "$BUILDX_BIN" "${NODO_BUILDX_SHA256:-}"
chmod +x "$BUILDX_BIN"
test -x "$BUILDX_BIN" || fail "buildx plugin not installed at ${BUILDX_BIN}"

echo "Isolated Docker installed:"
echo "  dockerd -> ${DOCKERD_BIN}"
echo "  docker  -> ${DOCKER_BIN}"
echo "  buildx  -> ${BUILDX_BIN}"
echo "nodo will start/stop this daemon automatically around each local pack."
