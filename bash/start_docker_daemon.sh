#!/bin/bash
# start_docker_daemon.sh
# Starts the isolated Docker daemon for nodo.
# This script should be run before starting the nodo service.

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
DOCKER_BIN="${BIN_DIR}/docker"
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
DOCKER_BIN="$(read_config_path_or_default '.dependencies.docker.BIN' "$DOCKER_BIN")"

if [ ! -x "${DOCKERD_BIN}" ]; then
    echo "Error: isolated dockerd not found at ${DOCKERD_BIN}. Run the installer."
    exit 1
fi

if [ ! -x "${DOCKER_BIN}" ]; then
    echo "Error: isolated docker client not found at ${DOCKER_BIN}. Run the installer."
    exit 1
fi

# Create directories if they don't exist
mkdir -p "${DOCKER_DATA_ROOT}"
mkdir -p "${DOCKER_CONFIG_DIR}"
mkdir -p "${DOCKER_EXEC_ROOT}"

# Ensure daemon.json exists and is compatible with our flags
ensure_daemon_config "${DOCKER_CONFIG_DIR}"

# Check if daemon is already running
if [ -f "${DOCKER_PID_FILE}" ]; then
    OLD_PID=$(cat "${DOCKER_PID_FILE}")
    if ps -p "${OLD_PID}" > /dev/null 2>&1; then
        echo "Nodo Docker daemon is already running (PID: ${OLD_PID})"
        exit 0
    else
        # PID file exists but process is dead, clean up
        rm -f "${DOCKER_PID_FILE}"
        rm -f "${DOCKER_SOCKET}"
    fi
fi

# Authoritative guard: scan for an isolated dockerd already bound to our
# data-root. The pidfile fast-path above can miss an orphan (stale/removed
# pidfile), and launching a second daemon on the same data-root collides on
# the boltdb lock ("metadata.db: timeout") and deletes the live socket.
RUNNING_PIDS="$(isolated_dockerd_pids "${DOCKER_DATA_ROOT}" || true)"
if [ -n "${RUNNING_PIDS}" ]; then
    if "${DOCKER_BIN}" -H "unix://${DOCKER_SOCKET}" info > /dev/null 2>&1; then
        # Idempotent: healthy daemon already serving this data-root.
        echo "Nodo Docker daemon already running and responsive (PID(s): ${RUNNING_PIDS})."
        exit 0
    else
        # Wedged: daemon alive but not answering. Do NOT delete the socket or
        # launch a second daemon — that is exactly what causes the collision.
        echo "Error: an isolated Docker daemon is already running on ${DOCKER_DATA_ROOT} (PID(s): ${RUNNING_PIDS}) but is not responding on ${DOCKER_SOCKET}."
        echo "It is holding the data-root lock, so a new daemon cannot start (this causes the 'metadata.db: timeout' error)."
        echo "Stop it first, then retry:"
        echo "    sudo \"${SCRIPT_DIR}/stop_docker_daemon.sh\" \"${TARGET_DIR}\""
        exit 1
    fi
fi

# Remove old socket if it exists (from crashed daemon)
rm -f "${DOCKER_SOCKET}"

echo "Starting isolated Docker daemon for nodo..."
echo "  Socket: ${DOCKER_SOCKET}"
echo "  Data root: ${DOCKER_DATA_ROOT}"
echo "  Exec root: ${DOCKER_EXEC_ROOT}"
echo "  Log file: ${DOCKER_LOG_FILE}"

# Start the Docker daemon with isolated configuration
DOCKER_PATH="$(dirname "${DOCKERD_BIN}"):$(dirname "${DOCKER_BIN}"):${PATH}"
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

# Dump the tail of the daemon log so the real dockerd error is visible.
dump_daemon_log() {
    echo "----- Last lines of ${DOCKER_LOG_FILE} -----"
    ${SUDO} tail -n 40 "${DOCKER_LOG_FILE}" 2>/dev/null || tail -n 40 "${DOCKER_LOG_FILE}" 2>/dev/null || true
    echo "--------------------------------------------"
}

nohup ${SUDO} env PATH="${DOCKER_PATH}" "${DOCKERD_BIN}" \
    --config-file="${DOCKER_CONFIG_DIR}/daemon.json" \
    -H "unix://${DOCKER_SOCKET}" \
    --pidfile="${DOCKER_PID_FILE}" \
    --data-root="${DOCKER_DATA_ROOT}" \
    --exec-root="${DOCKER_EXEC_ROOT}" \
    --userland-proxy=false \
    > "${DOCKER_LOG_FILE}" 2>&1 &

# Remember the PID of the launched background process so we can detect an
# early crash. Because dockerd is started via sudo, this is the PID of the
# sudo wrapper; the pidfile (written by dockerd itself) is used as a
# secondary liveness signal below.
DAEMON_LAUNCH_PID=$!

# Wait for the daemon to become responsive (max 30 seconds).
echo "Waiting for Docker daemon to start..."
for i in $(seq 1 30); do
    # Primary early-exit: if the launched process is gone the daemon crashed
    # during startup. Cross-check the pidfile in case sudo re-parented dockerd.
    if ! kill -0 "${DAEMON_LAUNCH_PID}" 2>/dev/null; then
        DAEMON_ALIVE=0
        if [ -f "${DOCKER_PID_FILE}" ]; then
            DOCKERD_PID="$(cat "${DOCKER_PID_FILE}" 2>/dev/null || true)"
            if [ -n "${DOCKERD_PID}" ] && ${SUDO} kill -0 "${DOCKERD_PID}" 2>/dev/null; then
                DAEMON_ALIVE=1
            fi
        fi
        if [ "${DAEMON_ALIVE}" -eq 0 ]; then
            echo "Error: Docker daemon process exited during startup."
            dump_daemon_log
            exit 1
        fi
    fi

    if [ -S "${DOCKER_SOCKET}" ]; then
        ${SUDO} chmod 666 "${DOCKER_SOCKET}" >/dev/null 2>&1 || true

        # Only report success once the daemon actually answers.
        if "${DOCKER_BIN}" -H "unix://${DOCKER_SOCKET}" info > /dev/null 2>&1; then
            echo "Nodo Docker daemon started successfully!"
            echo "Docker daemon is responsive."
            exit 0
        fi
    fi
    sleep 1
done

echo "Error: Docker daemon did not become responsive within 30 seconds."
dump_daemon_log
exit 1
