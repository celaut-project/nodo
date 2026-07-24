#!/bin/bash
# stop_docker_daemon.sh
# Stops the isolated Docker daemon for nodo.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/lib_docker_daemon.sh
. "${SCRIPT_DIR}/lib_docker_daemon.sh"

if [ -z "$1" ]; then
    echo "Error: TARGET_DIR is not provided."
    exit 1
fi

TARGET_DIR="$1"
DOCKER_DIR="${TARGET_DIR}/docker"
DOCKER_SOCKET="${DOCKER_DIR}/docker.sock"
DOCKER_PID_FILE="${DOCKER_DIR}/docker.pid"
DOCKER_DATA_ROOT="${DOCKER_DIR}/data"
DOCKER_EXEC_ROOT="${DOCKER_DIR}/exec"

# --- Pidfile-based shutdown (best effort) ---
# Falls through to the data-root sweep below so orphans the pidfile never
# tracked (stale/removed pidfile) are still caught.
if [ ! -f "${DOCKER_PID_FILE}" ]; then
    echo "No Docker daemon PID file found. Checking for orphaned daemons by data-root..."
else
    PID=$(cat "${DOCKER_PID_FILE}")
    if ! ps -p "${PID}" > /dev/null 2>&1; then
        echo "Docker daemon (PID: ${PID}) is not running. Cleaning up..."
    else
        echo "Stopping nodo Docker daemon (PID: ${PID})..."

        # First try a graceful shutdown
        kill "${PID}" 2>/dev/null || true

        # Wait up to 15 seconds for graceful shutdown
        for i in $(seq 1 15); do
            if ! ps -p "${PID}" > /dev/null 2>&1; then
                echo "Docker daemon stopped gracefully."
                break
            fi
            sleep 1
        done

        # Force kill if still running
        if ps -p "${PID}" > /dev/null 2>&1; then
            echo "Forcing daemon shutdown..."
            kill -9 "${PID}" 2>/dev/null || true
            sleep 2
        fi
    fi
fi

# Catch orphaned isolated daemons the pidfile didn't track
ORPHAN_PIDS="$(isolated_dockerd_pids "${DOCKER_DATA_ROOT}" || true)"
if [ -n "${ORPHAN_PIDS}" ]; then
    echo "Found running isolated Docker daemon(s) on ${DOCKER_DATA_ROOT}: ${ORPHAN_PIDS}. Stopping..."
    for p in ${ORPHAN_PIDS}; do kill "${p}" 2>/dev/null || true; done
    # wait up to 15s for graceful exit
    for i in $(seq 1 15); do
        still="$(isolated_dockerd_pids "${DOCKER_DATA_ROOT}" || true)"
        [ -z "${still}" ] && break
        sleep 1
    done
    still="$(isolated_dockerd_pids "${DOCKER_DATA_ROOT}" || true)"
    if [ -n "${still}" ]; then
        echo "Forcing shutdown of: ${still}"
        for p in ${still}; do kill -9 "${p}" 2>/dev/null || true; done
        sleep 2
    fi
fi
# Best-effort cleanup of leftover mounts + stale files
for m in "${DOCKER_DATA_ROOT}"/overlay2/*/merged "${DOCKER_EXEC_ROOT}"/netns/*; do
    [ -e "$m" ] && umount "$m" 2>/dev/null || true
done
rm -f "${DOCKER_PID_FILE}" "${DOCKER_SOCKET}"
echo "Isolated Docker daemon cleanup complete."
