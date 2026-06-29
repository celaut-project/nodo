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

# 1. Stop and disable services
stop_unit_if_exists "nodo.service"
stop_units_referencing_target_dir

# 2. (Removed) Docker container/daemon cleanup
# nodo no longer runs a local Docker daemon — services run as Cloud Hypervisor
# microVMs and are torn down with the service. There is nothing Docker-related
# to stop or unmount here.

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
    rm -rf "$TARGET_DIR"
fi

log "Uninstallation completed successfully."
