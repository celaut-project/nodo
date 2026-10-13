#!/bin/bash

set -euo pipefail

# Without sudo, this script removes what the user owns (the install directory,
# the wrapper, the shell completion) and collects the steps that need root. It
# prints them, saves them to <target-dir>/uninstall-privileged.sh, and asks before
# it runs them with sudo. This is the same method as install.sh
# (bash/lib_privileged.sh). With sudo, it does all of it itself.

usage() {
    cat <<EOF
Usage: ./uninstall.sh [options]

  --target-dir <path>  nodo install directory (default: /nodo with sudo,
                       ~/.local/share/nodo without).
  --no-input           Without sudo: print the root steps and save them; do not ask.
  -h, --help           Show this help message.
EOF
}

TARGET_DIR=""
INTERACTIVE=true

while [ $# -gt 0 ]; do
    case "$1" in
        --target-dir)
            if [ -z "${2:-}" ]; then
                printf "Error: --target-dir requires a path.\n" >&2
                exit 1
            fi
            TARGET_DIR="$2"
            shift 2
            ;;
        --no-input)
            INTERACTIVE=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf "Error: Unknown argument '%s'.\n" "$1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    UNPRIVILEGED=false
    : "${TARGET_DIR:=/nodo}"
    WRAPPER_SCRIPT="/usr/local/bin/nodo"
else
    UNPRIVILEGED=true
    : "${TARGET_DIR:=${XDG_DATA_HOME:-$HOME/.local/share}/nodo}"
    WRAPPER_SCRIPT="$HOME/.local/bin/nodo"
    # Debian leaves the sbin directories out of an ordinary user's PATH.
    PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
    export PATH
fi

SERVICE_FILE="/etc/systemd/system/nodo.service"

# The root-owned paths and the step list that install.sh uses. The library is
# read now, before anything is removed.
LIB_PRIVILEGED="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bash/lib_privileged.sh"
[ -f "$LIB_PRIVILEGED" ] || LIB_PRIVILEGED="$TARGET_DIR/bash/lib_privileged.sh"
if [ -f "$LIB_PRIVILEGED" ]; then
    # shellcheck source=bash/lib_privileged.sh
    . "$LIB_PRIVILEGED"
elif [ "$UNPRIVILEGED" = true ]; then
    printf "Error: bash/lib_privileged.sh was not found. Run this script from the nodo directory.\n" >&2
    exit 1
fi
NODO_SERVICE_USER_RECORD="${NODO_SERVICE_USER_RECORD:-/etc/nodo/service-user}"
NODO_VIRTIOFSD_SYSTEM_PATH="${NODO_VIRTIOFSD_SYSTEM_PATH:-/usr/local/libexec/nodo/virtiofsd}"
NODO_VIRTIOFSD_APPARMOR_PROFILE="${NODO_VIRTIOFSD_APPARMOR_PROFILE:-/etc/apparmor.d/nodo-virtiofsd}"

# The target is removed with rm -rf, and it can be any path now.
case "$TARGET_DIR" in
    /*) ;;
    *) TARGET_DIR="$(pwd)/$TARGET_DIR" ;;
esac
TARGET_DIR="$(realpath -ms -- "$TARGET_DIR")"
if [ "$TARGET_DIR" = / ] || [ "$TARGET_DIR" = "$HOME" ] \
    || { [ -d "$TARGET_DIR" ] && [ ! -f "$TARGET_DIR/nodo.py" ]; }; then
    printf "Error: %s is not a nodo install directory (no nodo.py in it). Use --target-dir.\n" "$TARGET_DIR" >&2
    exit 1
fi

log() {
    printf "%s\n" "$1"
}

unit_references_target_dir() {
    systemctl show -p ExecStart -p WorkingDirectory "$1" 2>/dev/null | grep -Fq "$TARGET_DIR"
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

stop_helper_processes() {
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

    # Stop the rootless BuildKit builder (current local-packer toolchain) and its
    # rootlesskit parent. Unlike the retired root dockerd, these belong to the
    # invoking user, so no privileged signal is needed to reach them.
    if pgrep -f "$TARGET_DIR/bin/buildkitd" >/dev/null 2>&1; then
        log "Stopping the rootless BuildKit builder..."
        pkill -f "$TARGET_DIR/bin/buildkitd" 2>/dev/null || true
        sleep 2

        if pgrep -f "$TARGET_DIR/bin/buildkitd" >/dev/null 2>&1; then
            log "Force killing the BuildKit builder..."
            pkill -9 -f "$TARGET_DIR/bin/buildkitd" 2>/dev/null || true
            sleep 1
        fi
    fi

    # Stop any helper processes that may still hold mounts
    pkill -f "$TARGET_DIR/docker" 2>/dev/null || true
    pkill -f "buildkitd-entrypoint" 2>/dev/null || true
    pkill -f "docker-init" 2>/dev/null || true
}

mountpoints_under_target_dir() {
    # Deepest first, so a mount is never removed from under another.
    command -v findmnt >/dev/null 2>&1 || return 0
    findmnt -rn -o TARGET | grep "^$TARGET_DIR" | sort -r || true
}

unmount_target_dir() {
    local mountpoint
    while read -r mountpoint; do
        [ -z "$mountpoint" ] && continue

        log "Unmounting $mountpoint..."
        umount "$mountpoint" 2>/dev/null || umount -l "$mountpoint" 2>/dev/null || true
    done < <(mountpoints_under_target_dir)
}

remove_root_files() {
    # What only root installed, and only a node that does not run as root has:
    # the service user record, the root-owned virtiofsd and its AppArmor profile.
    if [ -e "$NODO_SERVICE_USER_RECORD" ]; then
        log "Removing $NODO_SERVICE_USER_RECORD..."
        rm -f "$NODO_SERVICE_USER_RECORD"
        rmdir --ignore-fail-on-non-empty "$(dirname "$NODO_SERVICE_USER_RECORD")" 2>/dev/null || true
    fi
    if [ -e "$NODO_VIRTIOFSD_APPARMOR_PROFILE" ]; then
        log "Removing the AppArmor profile $NODO_VIRTIOFSD_APPARMOR_PROFILE..."
        apparmor_parser -R "$NODO_VIRTIOFSD_APPARMOR_PROFILE" 2>/dev/null || true
        rm -f "$NODO_VIRTIOFSD_APPARMOR_PROFILE"
    fi
    if [ -e "$NODO_VIRTIOFSD_SYSTEM_PATH" ]; then
        log "Removing $NODO_VIRTIOFSD_SYSTEM_PATH..."
        rm -f "$NODO_VIRTIOFSD_SYSTEM_PATH"
        rmdir --ignore-fail-on-non-empty "$(dirname "$NODO_VIRTIOFSD_SYSTEM_PATH")" 2>/dev/null || true
    fi
}

remove_user_completion() {
    # The two files that completion.py installs without root.
    local path
    for path in \
        "$HOME/.local/share/bash-completion/completions/nodo" \
        "$HOME/.zsh/completions/_nodo"; do
        if [ -f "$path" ]; then
            log "Removing shell completion $path..."
            rm -f "$path"
        fi
    done
}

remove_wrapper() {
    if [ -f "$WRAPPER_SCRIPT" ]; then
        log "Removing wrapper script $WRAPPER_SCRIPT..."
        rm -f "$WRAPPER_SCRIPT"
    fi
}

# -----------------------------------------------------------------------------
# Without sudo
# -----------------------------------------------------------------------------

uninstall_unprivileged() {
    local privileged_script="$TARGET_DIR/uninstall-privileged.sh"
    local unit units=() mountpoint

    NODO_PRIV_FILE="$(mktemp)"
    export NODO_PRIV_FILE
    trap 'rm -f "$NODO_PRIV_FILE"' EXIT

    # The user's own processes first: they can hold mounts under the target.
    stop_helper_processes

    # Each step is recorded only when it is needed, so a second run (after the
    # operator ran the saved script) finds nothing left to ask for.
    if command -v systemctl >/dev/null 2>&1; then
        for unit in $(systemctl list-units --type=service --all --no-legend 2>/dev/null | awk '{print $1}'); do
            if [ "$unit" = nodo.service ] || unit_references_target_dir "$unit"; then
                units+=("$unit")
            fi
        done
        if [ "${#units[@]}" -gt 0 ]; then
            priv_record "Stop and disable ${units[*]} (this stops every VM it runs)" \
                "sudo systemctl disable --now ${units[*]}"
        fi
    fi

    if [ -f "$SERVICE_FILE" ] && grep -Fq "$TARGET_DIR" "$SERVICE_FILE"; then
        priv_record "Remove the systemd unit" \
            "sudo rm -f $(printf '%q' "$SERVICE_FILE") && sudo systemctl daemon-reload"
    fi

    if [ -e "$NODO_SERVICE_USER_RECORD" ]; then
        priv_record "Remove the record of the user nodo runs as" \
            "sudo rm -f $(printf '%q' "$NODO_SERVICE_USER_RECORD") && sudo rmdir --ignore-fail-on-non-empty $(printf '%q' "$(dirname "$NODO_SERVICE_USER_RECORD")")"
    fi

    if [ -e "$NODO_VIRTIOFSD_APPARMOR_PROFILE" ]; then
        priv_record "Unload and remove the AppArmor profile for virtiofsd" \
            "{ sudo apparmor_parser -R $(printf '%q' "$NODO_VIRTIOFSD_APPARMOR_PROFILE") || true; } && sudo rm -f $(printf '%q' "$NODO_VIRTIOFSD_APPARMOR_PROFILE")"
    fi
    if [ -e "$NODO_VIRTIOFSD_SYSTEM_PATH" ]; then
        priv_record "Remove the root-owned copy of virtiofsd" \
            "sudo rm -f $(printf '%q' "$NODO_VIRTIOFSD_SYSTEM_PATH") && sudo rmdir --ignore-fail-on-non-empty $(printf '%q' "$(dirname "$NODO_VIRTIOFSD_SYSTEM_PATH")")"
    fi

    # Only root can unmount. rm -rf must never run on a tree with a mount in it.
    while read -r mountpoint; do
        [ -n "$mountpoint" ] || continue
        priv_record "Unmount $mountpoint" \
            "sudo umount $(printf '%q' "$mountpoint") || sudo umount -l $(printf '%q' "$mountpoint")"
    done < <(mountpoints_under_target_dir)

    if [ -d "$TARGET_DIR" ]; then
        priv_offer "These steps need root. Nothing is removed from $TARGET_DIR until they are done:" \
            "$INTERACTIVE" "$privileged_script" || {
            case $? in
                2) printf "\nThe steps that did not run are in %s.\n" "$privileged_script" >&2 ;;
                *) printf "\nRun them, then run this script again to remove %s: bash %s\n" \
                    "$TARGET_DIR" "$privileged_script" ;;
            esac
            exit 1
        }
    elif priv_pending; then
        priv_offer "These steps need root:" "$INTERACTIVE" "" || exit 1
    fi

    # What the user owns.
    remove_wrapper
    remove_user_completion

    if [ -d "$TARGET_DIR" ]; then
        log "Removing project directory $TARGET_DIR..."
        cd /
        if ! rm -rf -- "$TARGET_DIR" 2>/dev/null; then
            # A file that root wrote there, such as a .pyc after 'sudo nodo doctor'.
            priv_record "Remove what root left in $TARGET_DIR" "sudo rm -rf -- $(printf '%q' "$TARGET_DIR")"
            priv_offer "Some files in $TARGET_DIR belong to root:" "$INTERACTIVE" "" \
                || { printf "Error: %s was not removed.\n" "$TARGET_DIR" >&2; exit 1; }
        fi
    fi

    log "Uninstallation completed successfully."
}

# -----------------------------------------------------------------------------
# With sudo
# -----------------------------------------------------------------------------

uninstall_privileged() {
    # Stop services
    stop_unit_if_exists "nodo.service"
    stop_units_referencing_target_dir

    # Cleanup legacy embedded Docker installations
    stop_helper_processes
    unmount_target_dir

    # Remove systemd service
    if [ -f "$SERVICE_FILE" ]; then
        log "Removing service file $SERVICE_FILE..."
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
    fi

    remove_root_files
    remove_wrapper

    # Remove installation directory
    if [ -d "$TARGET_DIR" ]; then
        log "Removing project directory $TARGET_DIR..."
        cd /
        rm -rf "$TARGET_DIR"
    fi

    log "Uninstallation completed successfully."
}

if [ "$UNPRIVILEGED" = true ]; then
    uninstall_unprivileged
else
    uninstall_privileged
fi
