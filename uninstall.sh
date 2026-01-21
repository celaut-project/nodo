#!/bin/bash

# Check if the script is running with root privileges
if [ "$(id -u)" -ne 0 ]; then
  printf "Error: This script needs to be run with sudo.\nPlease run: sudo $0\n" >&2
  exit 1
fi

TARGET_DIR="/nodo"
SERVICE_FILE="/etc/systemd/system/nodo.service"
WRAPPER_SCRIPT="/usr/local/bin/nodo"

# 1. Stop and disable service
if systemctl list-units --full -all | grep -Fq "nodo.service"; then
    printf "Stopping and disabling nodo.service...\n"
    systemctl stop nodo.service
    systemctl disable nodo.service
else
    printf "nodo.service not found or already stopped.\n"
fi

# 1b. Stop isolated Docker daemon if running
DOCKER_PID_FILE="$TARGET_DIR/docker/docker.pid"
if [ -f "$DOCKER_PID_FILE" ]; then
    DOCKER_PID=$(cat "$DOCKER_PID_FILE")
    if ps -p "$DOCKER_PID" > /dev/null 2>&1; then
        printf "Stopping isolated Docker daemon (PID: $DOCKER_PID)...\n"
        kill "$DOCKER_PID" 2>/dev/null || true
        sleep 2
        # Force kill if still running
        if ps -p "$DOCKER_PID" > /dev/null 2>&1; then
            kill -9 "$DOCKER_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$DOCKER_PID_FILE"
fi

# 2. Remove containers
# We need to do this before removing the directory because we need the python env and code to identify containers.
if [ -d "$TARGET_DIR" ]; then
    printf "Attempting to clean up Docker containers created by the node...\n"
    
    CLEANUP_SCRIPT="$TARGET_DIR/cleanup_containers.py"
    
    # Create a temporary python script to clean containers
    cat <<EOF > "$CLEANUP_SCRIPT"
import sys
import os
import docker

# Add src to path
sys.path.append("$TARGET_DIR")

try:
    # Initialize ConfigManager to ensure paths are correct
    # It reads from config.yaml in MAIN_DIR (which defaults to /nodo or env var)
    os.environ["MAIN_DIR"] = "$TARGET_DIR"
    os.chdir("$TARGET_DIR") # Change cwd so ConfigManager finds config.yaml

    from src.database.sql_connection import SQLConnection
    from src.utils.config import ConfigManager, DOCKER_CLIENT
    
    # Force load config
    ConfigManager()
    
    sc = SQLConnection()
    # Use the isolated Docker client from nodo's config
    client = DOCKER_CLIENT()
    
    container_ids = sc.get_all_internal_containers_ids()
    print(f"Found {len(container_ids)} containers to remove.")
    
    for cid in container_ids:
        try:
            container = client.containers.get(cid)
            print(f"Stopping and removing container {cid}...")
            container.stop(timeout=1)
            container.remove(force=True)
        except docker.errors.NotFound:
            print(f"Container {cid} not found (already removed).")
        except Exception as e:
            print(f"Error removing container {cid}: {e}")

except Exception as e:
    print(f"Error during container cleanup: {e}")
EOF

    # Run the cleanup script using the venv
    if [ -f "$TARGET_DIR/venv/bin/activate" ]; then
        printf "Using virtual environment at $TARGET_DIR/venv to run cleanup...\n"
        # We use a subshell to avoid polluting the current shell environment
        (
            source "$TARGET_DIR/venv/bin/activate"
            python3 "$CLEANUP_SCRIPT"
        )
    else
        printf "Warning: Virtual environment not found at $TARGET_DIR/venv. Skipping container cleanup via DB.\n"
        printf "You may need to manually remove Docker containers created by this node.\n"
    fi
    
    rm -f "$CLEANUP_SCRIPT"
else
    printf "Target directory $TARGET_DIR not found. Skipping container cleanup.\n"
fi

# 3. Remove service file
if [ -f "$SERVICE_FILE" ]; then
    printf "Removing service file $SERVICE_FILE...\n"
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
fi

# 4. Remove wrapper script
if [ -f "$WRAPPER_SCRIPT" ]; then
    printf "Removing wrapper script $WRAPPER_SCRIPT...\n"
    rm -f "$WRAPPER_SCRIPT"
fi

# 5. Remove project directory
if [ -d "$TARGET_DIR" ]; then
    printf "Removing project directory $TARGET_DIR...\n"
    rm -rf "$TARGET_DIR"
fi

printf "Uninstallation completed successfully.\n"
