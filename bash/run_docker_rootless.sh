#!/bin/bash
NODO_DIR="${1:-$HOME/.nodo}"
DOCKER_DIR="$NODO_DIR/docker"
BIN_DIR="$DOCKER_DIR/bin"

export PATH="$BIN_DIR:$PATH"
export DOCKER_HOST="unix://$DOCKER_DIR/docker.sock"

# Allow containers to access host services (needed for gateway communication)
# This disables host loopback isolation so containers can reach the node gateway
export DOCKERD_ROOTLESS_ROOTLESSKIT_DISABLE_HOST_LOOPBACK=false

# Launch the isolated Docker daemon
# --exec-root and --data-root ensure it doesn't use the default Docker folders
if [ ! -f "$DOCKER_DIR/docker.pid" ] || ! kill -0 $(cat "$DOCKER_DIR/docker.pid") 2>/dev/null; then
    echo "Starting Docker Rootless..."
    "$BIN_DIR/dockerd-rootless.sh" \
        --exec-root "$DOCKER_DIR/run" \
        --data-root "$DOCKER_DIR/data" \
        --pidfile "$DOCKER_DIR/docker.pid" \
        --host "$DOCKER_HOST" > "$DOCKER_DIR/docker.log" 2>&1 &
    
    # Wait for the socket to be ready
    echo "Waiting for Docker to be ready..."
    MAX_RETRIES=30
    COUNT=0
    while [ ! -S "$DOCKER_DIR/docker.sock" ] && [ $COUNT -lt $MAX_RETRIES ]; do
        if ! kill -0 $! 2>/dev/null; then
            echo "Error: Docker process died. Check $DOCKER_DIR/docker.log"
            exit 1
        fi
        sleep 1
        COUNT=$((COUNT + 1))
    done


    if [ ! -S "$DOCKER_DIR/docker.sock" ]; then
        echo "Error: Docker failed to start. Check $DOCKER_DIR/docker.log"
        exit 1
    fi
    echo "Docker Rootless is ready at $DOCKER_HOST"
else
    echo "Docker Rootless is already running."
fi
