#!/bin/bash

# Check if the script is running with root privileges
if [ "$(id -u)" -ne 0 ]; then
  printf "Error: This script needs to be run with sudo.\nPlease run: sudo $0\n" >&2
  exit 1
fi

TARGET_DIR="/nodo"
SERVICE_FILE="/etc/systemd/system/nodo.service"
WRAPPER_SCRIPT="/usr/local/bin/nodo"

log() {
  printf "%s\n" "$1"
}

stop_unit_if_exists() {
  local unit="$1"
  if systemctl list-units --full -all | grep -Fq "$unit"; then
    log "Stopping and disabling $unit..."
    systemctl stop "$unit" 2>/dev/null || true
    systemctl disable "$unit" 2>/dev/null || true
  fi
}

stop_units_referencing_target_dir() {
  log "Scanning for systemd services referencing $TARGET_DIR..."
  local units
  units=$(systemctl list-units --type=service --all --no-legend 2>/dev/null | awk '{print $1}')
  for unit in $units; do
    if systemctl show -p ExecStart "$unit" 2>/dev/null | grep -Fq "$TARGET_DIR"; then
      stop_unit_if_exists "$unit"
    fi
  done
}

kill_dockerd_under_target_dir() {
  local pids
  pids=$(pgrep -f "$TARGET_DIR/.*/dockerd" || true)
  if [ -n "$pids" ]; then
    log "Stopping dockerd processes under $TARGET_DIR: $pids"
    kill $pids 2>/dev/null || true
    sleep 2
    # Force kill if still running
    for pid in $pids; do
      if ps -p "$pid" >/dev/null 2>&1; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    done
  fi
}

unmount_netns() {
  local netns_dir="$TARGET_DIR/docker/exec/netns"
  if [ -d "$netns_dir" ]; then
    log "Unmounting docker netns mounts under $netns_dir..."
    if command -v findmnt >/dev/null 2>&1; then
      # Unmount deepest targets first to avoid dependency issues
      while read -r mp; do
        [ -n "$mp" ] && umount -l "$mp" 2>/dev/null || true
      done < <(findmnt -R -n -o TARGET "$netns_dir" 2>/dev/null | sort -r)
    else
      if mount | grep -Fq "$netns_dir"; then
        umount -l "$netns_dir" 2>/dev/null || true
      fi
    fi
  fi
}

# 1. Stop and disable services
stop_unit_if_exists "nodo.service"
stop_units_referencing_target_dir

# 2. Remove containers (best-effort) before stopping Docker
if [ -d "$TARGET_DIR" ]; then
    log "Attempting to clean up Docker containers created by the node..."
    
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
    from src.utils.config import ConfigManager
    from src.utils.runtime import DOCKER_CLIENT
    
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
        log "Using virtual environment at $TARGET_DIR/venv to run cleanup..."
        # We use a subshell to avoid polluting the current shell environment
        (
            source "$TARGET_DIR/venv/bin/activate"
            python3 "$CLEANUP_SCRIPT"
        )
    else
        log "Warning: Virtual environment not found at $TARGET_DIR/venv. Skipping container cleanup via DB."
        log "You may need to manually remove Docker containers created by this node."
    fi
    
    rm -f "$CLEANUP_SCRIPT"
else
    log "Target directory $TARGET_DIR not found. Skipping container cleanup."
fi

# 3. Stop isolated Docker daemon if running
DOCKER_PID_FILE="$TARGET_DIR/docker/docker.pid"
if [ -f "$DOCKER_PID_FILE" ]; then
    DOCKER_PID=$(cat "$DOCKER_PID_FILE")
    if ps -p "$DOCKER_PID" > /dev/null 2>&1; then
        log "Stopping isolated Docker daemon (PID: $DOCKER_PID)..."
        kill "$DOCKER_PID" 2>/dev/null || true
        sleep 2
        # Force kill if still running
        if ps -p "$DOCKER_PID" > /dev/null 2>&1; then
            kill -9 "$DOCKER_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$DOCKER_PID_FILE"
fi

# 3b. Stop any remaining dockerd processes under /nodo
kill_dockerd_under_target_dir

# 3c. Verify isolated Docker daemon is stopped and warn if sockets remain
DOCKER_SOCKET="$TARGET_DIR/docker/docker.sock"
if pgrep -f "$TARGET_DIR/docker" >/dev/null 2>&1; then
    log "Warning: Detected docker-related processes still running under $TARGET_DIR."
    log "You may need to stop them manually before removing the directory."
fi
if [ -S "$DOCKER_SOCKET" ]; then
    log "Warning: Docker socket still exists at $DOCKER_SOCKET (daemon may still be running)."
fi

# 4. Unmount any leftover netns mounts to avoid "Device or resource busy"
unmount_netns

# 5. Remove service file
if [ -f "$SERVICE_FILE" ]; then
    log "Removing service file $SERVICE_FILE..."
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
fi

# 6. Remove wrapper script
if [ -f "$WRAPPER_SCRIPT" ]; then
    log "Removing wrapper script $WRAPPER_SCRIPT..."
    rm -f "$WRAPPER_SCRIPT"
fi

# 7. Remove project directory
if [ -d "$TARGET_DIR" ]; then
    log "Removing project directory $TARGET_DIR..."
    sudo umount /nodo/docker/exec/netns/default
    rm -rf "$TARGET_DIR"
fi

log "Uninstallation completed successfully."
