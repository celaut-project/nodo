#!/bin/bash
# stop_buildkit_daemon.sh
# Stops nodo's rootless BuildKit builder.
#
# No sudo anywhere: the builder runs as the invoking user, so `kill` always
# reaches it. The guards below are kept as defence in depth — the previous
# root dockerd could not be signalled at all from an unprivileged pack, and
# deleting a live daemon's socket wedged it permanently.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/lib_rootless.sh
. "${SCRIPT_DIR}/lib_rootless.sh"

if [ -z "$1" ]; then
    echo "Error: TARGET_DIR is not provided."
    exit 1
fi

TARGET_DIR="$1"
BUILDKIT_DIR="${TARGET_DIR}/buildkit"
BUILDKIT_ROOT="${BUILDKIT_DIR}/data"
BUILDKIT_SOCKET="${BUILDKIT_DIR}/buildkitd.sock"
BUILDKIT_PID_FILE="${BUILDKIT_DIR}/buildkitd.pid"

# --- Pidfile-based shutdown (best effort) ---
# Falls through to the --root sweep below so a builder the pidfile never tracked
# (stale or removed pidfile) is still caught.
if [ ! -f "${BUILDKIT_PID_FILE}" ]; then
    echo "No BuildKit PID file found. Checking for orphaned builders by root..."
else
    PID=$(cat "${BUILDKIT_PID_FILE}")
    if ! ps -p "${PID}" > /dev/null 2>&1; then
        echo "BuildKit builder (PID: ${PID}) is not running. Cleaning up..."
    else
        echo "Stopping nodo BuildKit builder (PID: ${PID})..."
        kill "${PID}" 2>/dev/null || true

        for i in $(seq 1 15); do
            if ! ps -p "${PID}" > /dev/null 2>&1; then
                echo "BuildKit builder stopped gracefully."
                break
            fi
            sleep 1
        done

        if ps -p "${PID}" > /dev/null 2>&1; then
            echo "Forcing builder shutdown..."
            kill -9 "${PID}" 2>/dev/null || true
            sleep 2
        fi
    fi
fi

# Catch orphans the pidfile didn't track (both buildkitd and its rootlesskit parent).
ORPHAN_PIDS="$(buildkitd_pids "${BUILDKIT_ROOT}" || true)"
if [ -n "${ORPHAN_PIDS}" ]; then
    echo "Found running BuildKit builder(s) on ${BUILDKIT_ROOT}: ${ORPHAN_PIDS}. Stopping..."
    for p in ${ORPHAN_PIDS}; do kill "${p}" 2>/dev/null || true; done
    for i in $(seq 1 15); do
        still="$(buildkitd_pids "${BUILDKIT_ROOT}" || true)"
        [ -z "${still}" ] && break
        sleep 1
    done
    still="$(buildkitd_pids "${BUILDKIT_ROOT}" || true)"
    if [ -n "${still}" ]; then
        echo "Forcing shutdown of: ${still}"
        for p in ${still}; do kill -9 "${p}" 2>/dev/null || true; done
        sleep 2
    fi
fi

# Never remove the socket while a builder is still listening on it: the daemon
# does not recreate a deleted socket, so deleting it would leave it alive but
# unreachable and every later pack would have to clear it first.
REMAINING="$(buildkitd_pids "${BUILDKIT_ROOT}" || true)"
if [ -n "${REMAINING}" ]; then
    echo "Error: the BuildKit builder on ${BUILDKIT_ROOT} is still running (PID(s): ${REMAINING}); it could not be stopped."
    echo "Keeping ${BUILDKIT_SOCKET} in place so the builder stays reachable."
    exit 1
fi

rm -f "${BUILDKIT_PID_FILE}" "${BUILDKIT_SOCKET}"
echo "Rootless BuildKit builder cleanup complete."
