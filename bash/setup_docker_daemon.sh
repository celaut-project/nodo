#!/bin/bash
# setup_docker_daemon.sh
# Sets up an isolated Docker daemon for the nodo project.
# This ensures nodo's containers are separate from the user's regular Docker environment.

set -e

if [ -z "$1" ]; then
    echo "Error: TARGET_DIR is not provided."
    exit 1
fi

TARGET_DIR="$1"
DOCKER_DIR="${TARGET_DIR}/docker"
DOCKER_DATA_ROOT="${DOCKER_DIR}/data"
DOCKER_SOCKET="${DOCKER_DIR}/docker.sock"
DOCKER_PID_FILE="${DOCKER_DIR}/docker.pid"
DOCKER_CONFIG_DIR="${DOCKER_DIR}/config"
DOCKER_LOG_FILE="${DOCKER_DIR}/dockerd.log"

echo "Setting up isolated Docker daemon for nodo..."
echo "  Docker directory: ${DOCKER_DIR}"
echo "  Data root: ${DOCKER_DATA_ROOT}"
echo "  Socket: ${DOCKER_SOCKET}"

# Create required directories
mkdir -p "${DOCKER_DATA_ROOT}"
mkdir -p "${DOCKER_CONFIG_DIR}"

# Create the daemon.json configuration file for the isolated Docker daemon
cat > "${DOCKER_CONFIG_DIR}/daemon.json" <<EOF
{
    "data-root": "${DOCKER_DATA_ROOT}",
    "storage-driver": "overlay2",
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    },
    "ipv6": false
}
EOF

echo "Docker daemon configuration created at ${DOCKER_CONFIG_DIR}/daemon.json"

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
echo "  dockerd --config-file=${DOCKER_CONFIG_DIR}/daemon.json -H unix://${DOCKER_SOCKET} --pidfile=${DOCKER_PID_FILE}"
