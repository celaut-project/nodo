#!/bin/bash
# setup_docker_daemon.sh
# Sets up an isolated Docker daemon for the nodo project.
# This ensures nodo's containers are separate from the user's regular Docker environment.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/lib_docker_daemon.sh
. "${SCRIPT_DIR}/lib_docker_daemon.sh"

if [ -z "$1" ]; then
    echo "Error: TARGET_DIR is not provided."
    exit 1
fi

TARGET_DIR="$1"
BIN_DIR="${TARGET_DIR}/bin"
DOCKERD_BIN="${BIN_DIR}/dockerd"
DOCKER_DIR="${TARGET_DIR}/docker"
DOCKER_DATA_ROOT="${DOCKER_DIR}/data"
DOCKER_SOCKET="${DOCKER_DIR}/docker.sock"
DOCKER_PID_FILE="${DOCKER_DIR}/docker.pid"
DOCKER_CONFIG_DIR="${DOCKER_DIR}/config"
DOCKER_LOG_FILE="${DOCKER_DIR}/dockerd.log"
DOCKER_EXEC_ROOT="${DOCKER_DIR}/exec"

expand_main_dir_placeholder() {
    printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"
}

read_config_path_or_default() {
    local query="$1"
    local default_value="$2"
    local yq_bin="${TARGET_DIR}/bin/yq"
    local config_file="${TARGET_DIR}/config.yaml"
    local value=""

    if [ -x "$yq_bin" ] && [ -f "$config_file" ]; then
        value="$("$yq_bin" -r "$query // \"\"" "$config_file" 2>/dev/null || true)"
    fi

    if [ -z "$value" ] || [ "$value" = "null" ]; then
        value="$default_value"
    fi

    expand_main_dir_placeholder "$value"
}

DOCKERD_BIN="$(read_config_path_or_default '.dependencies.docker.DAEMON_BIN' "$DOCKERD_BIN")"

echo "Setting up isolated Docker daemon for nodo..."
echo "  Docker directory: ${DOCKER_DIR}"
echo "  Data root: ${DOCKER_DATA_ROOT}"
echo "  Socket: ${DOCKER_SOCKET}"

# Create required directories
mkdir -p "${DOCKER_DATA_ROOT}"
mkdir -p "${DOCKER_CONFIG_DIR}"
mkdir -p "${DOCKER_EXEC_ROOT}"

# Ensure daemon.json exists and is compatible with our flags
ensure_daemon_config "${DOCKER_CONFIG_DIR}"

echo "Docker daemon configuration ensured at ${DOCKER_CONFIG_DIR}/daemon.json"

# Check if there's already a nodo Docker daemon running
if [ -f "${DOCKER_PID_FILE}" ]; then
    OLD_PID=$(cat "${DOCKER_PID_FILE}")
    if ps -p "${OLD_PID}" > /dev/null 2>&1; then
        echo "Stopping existing nodo Docker daemon (PID: ${OLD_PID})..."
        kill "${OLD_PID}" 2>/dev/null || true
        sleep 2
        # Force kill if still running
        if ps -p "${OLD_PID}" > /dev/null 2>&1; then
            kill -9 "${OLD_PID}" 2>/dev/null || true
        fi
    fi
    rm -f "${DOCKER_PID_FILE}"
fi

# Remove old socket if it exists
rm -f "${DOCKER_SOCKET}"

echo "Setup complete. The nodo Docker daemon will be started when the nodo service runs."
echo ""
echo "Environment variables to use this Docker daemon:"
echo "  DOCKER_HOST=unix://${DOCKER_SOCKET}"
echo ""
echo "To manually start the daemon:"
echo "  ${DOCKERD_BIN} --config-file=${DOCKER_CONFIG_DIR}/daemon.json -H unix://${DOCKER_SOCKET} --pidfile=${DOCKER_PID_FILE} --data-root=${DOCKER_DATA_ROOT} --exec-root=${DOCKER_EXEC_ROOT}"
