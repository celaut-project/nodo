#!/bin/bash
# start_docker_daemon.sh
# Starts the isolated Docker daemon for nodo.
# This script should be run before starting the nodo service.

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

# Create directories if they don't exist
mkdir -p "${DOCKER_DATA_ROOT}"
mkdir -p "${DOCKER_CONFIG_DIR}"

# Create daemon.json if it doesn't exist
if [ ! -f "${DOCKER_CONFIG_DIR}/daemon.json" ]; then
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
fi

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

# Remove old socket if it exists (from crashed daemon)
rm -f "${DOCKER_SOCKET}"

echo "Starting isolated Docker daemon for nodo..."
echo "  Socket: ${DOCKER_SOCKET}"
echo "  Data root: ${DOCKER_DATA_ROOT}"
echo "  Log file: ${DOCKER_LOG_FILE}"

# Start the Docker daemon with isolated configuration
# Note: containerd can be shared with the system, but everything else is isolated
nohup dockerd \
    --config-file="${DOCKER_CONFIG_DIR}/daemon.json" \
    -H "unix://${DOCKER_SOCKET}" \
    --pidfile="${DOCKER_PID_FILE}" \
    > "${DOCKER_LOG_FILE}" 2>&1 &

# Wait for the socket to be created (max 30 seconds)
echo "Waiting for Docker daemon to start..."
for i in $(seq 1 30); do
    if [ -S "${DOCKER_SOCKET}" ]; then
        echo "Nodo Docker daemon started successfully!"
        
        # Verify it works
        if docker -H "unix://${DOCKER_SOCKET}" info > /dev/null 2>&1; then
            echo "Docker daemon is responsive."
            exit 0
        fi
    fi
    sleep 1
done

echo "Error: Docker daemon failed to start within 30 seconds."
echo "Check the log file: ${DOCKER_LOG_FILE}"
exit 1
