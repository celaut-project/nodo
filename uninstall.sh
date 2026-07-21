#!/bin/bash

set -euo pipefail

# Check if the script is running with root privileges
if [ "$(id -u)" -ne 0 ]; then
    printf "Error: This script needs to be run with sudo.\nPlease run: sudo %s\n" "$0" >&2
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

    if systemctl list-units --full -all 2>/dev/null | grep -Fq "$unit"; then
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

cleanup_legacy_docker() {
    log "Checking for legacy embedded Docker..."

    # Stop any dockerd running from /nodo
    if pgrep -f "$TARGET_DIR/bin/dockerd" >/dev/null 2>&1; then
        log "Stopping embedded dockerd..."
        pkill -f "$TARGET_DIR/bin/dockerd" 2>/dev/null || true
        sleep 2

        if pgrep -f "$TARGET_DIR/bin/dockerd" >/dev/null 2>&1; then
            log "Force killing embedded dockerd..."
            pkill -9 -f "$TARGET_DIR/bin/dockerd" 2>/dev/null || true
            sleep 1
        fi
    fi

    # Stop any helper processes that may still hold mounts
    pkill -f "$TARGET_DIR/docker" 2>/dev/null || true
    pkill -f "buildkitd-entrypoint" 2>/dev/null || true
    pkill -f "docker-init" 2>/dev/null || true

    # Unmount every mountpoint under /nodo (deepest first)
    if command -v findmnt >/dev/null 2>&1; then
        while read -r mountpoint; do
            [ -z "$mountpoint" ] && continue

            log "Unmounting $mountpoint..."
            umount "$mountpoint" 2>/dev/null || umount -l "$mountpoint" 2>/dev/null || true
        done < <(
            findmnt -rn -o TARGET | grep "^$TARGET_DIR" | sort -r
        )
    fi
}

# -----------------------------------------------------------------------------
# Stop services
# -----------------------------------------------------------------------------

stop_unit_if_exists "nodo.service"
stop_units_referencing_target_dir

# -----------------------------------------------------------------------------
# Cleanup legacy embedded Docker installations
# -----------------------------------------------------------------------------

cleanup_legacy_docker

# -----------------------------------------------------------------------------
# Remove systemd service
# -----------------------------------------------------------------------------

if [ -f "$SERVICE_FILE" ]; then
    log "Removing service file $SERVICE_FILE..."
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
fi

# -----------------------------------------------------------------------------
# Remove wrapper script
# -----------------------------------------------------------------------------

if [ -f "$WRAPPER_SCRIPT" ]; then
    log "Removing wrapper script $WRAPPER_SCRIPT..."
    rm -f "$WRAPPER_SCRIPT"
fi

# -----------------------------------------------------------------------------
# Remove installation directory
# -----------------------------------------------------------------------------

if [ -d "$TARGET_DIR" ]; then
    log "Removing project directory $TARGET_DIR..."
    rm -rf "$TARGET_DIR"
fi

log "Uninstallation completed successfully."