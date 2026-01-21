#!/bin/bash
# stop_docker_daemon.sh
# Stops the isolated Docker daemon for nodo.

set -e

if [ -z "$1" ]; then
    echo "Error: TARGET_DIR is not provided."
    exit 1
fi

TARGET_DIR="$1"
DOCKER_DIR="${TARGET_DIR}/docker"
DOCKER_SOCKET="${DOCKER_DIR}/docker.sock"
DOCKER_PID_FILE="${DOCKER_DIR}/docker.pid"

if [ ! -f "${DOCKER_PID_FILE}" ]; then
    echo "No Docker daemon PID file found. Daemon may not be running."
    exit 0
fi

PID=$(cat "${DOCKER_PID_FILE}")

if ! ps -p "${PID}" > /dev/null 2>&1; then
    echo "Docker daemon (PID: ${PID}) is not running. Cleaning up..."
    rm -f "${DOCKER_PID_FILE}"
    rm -f "${DOCKER_SOCKET}"
    exit 0
fi

echo "Stopping nodo Docker daemon (PID: ${PID})..."

# First try a graceful shutdown
kill "${PID}" 2>/dev/null || true

# Wait up to 15 seconds for graceful shutdown
for i in $(seq 1 15); do
    if ! ps -p "${PID}" > /dev/null 2>&1; then
        echo "Docker daemon stopped gracefully."
        rm -f "${DOCKER_PID_FILE}"
        rm -f "${DOCKER_SOCKET}"
        exit 0
    fi
    sleep 1
done

# Force kill if still running
echo "Forcing daemon shutdown..."
kill -9 "${PID}" 2>/dev/null || true
sleep 2

rm -f "${DOCKER_PID_FILE}"
rm -f "${DOCKER_SOCKET}"
echo "Docker daemon stopped."
