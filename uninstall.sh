#!/bin/bash

set -euo pipefail

# Without sudo, this script removes what the user owns (the install directory,
# the wrapper, the shell completion) and collects the steps that need root. It
# prints them, saves them to <target-dir>/uninstall-privileged.sh, and asks before
# it runs them with sudo. This is the same method as install.sh
# (bash/lib_privileged.sh). With sudo, it does all of it itself.
#
# In both modes it first prints what it will remove and what it keeps, and asks.
# It only touches what names the target directory: the systemd unit, the wrapper
# and the root files of the node that unit runs belong to another install when
# they name another directory, and are kept. The wallet mnemonics in config.yaml
# and its backups are kept too, moved out of the tree, unless --delete-wallets.

usage() {
    cat <<EOF
Usage: ./uninstall.sh [options]

  --target-dir <path>  nodo install directory (default: /nodo with sudo,
                       ~/.local/share/nodo without).
  -y, --yes            Do not ask before removing. Without a terminal to ask at,
                       the script removes nothing unless this is given.
  --no-input           Ask nothing: without sudo, print the root steps and save
                       them. Removes nothing unless --yes is given too.
  --delete-wallets     Also delete the wallet mnemonics (config.yaml and its
                       backups). Asks for a typed confirmation at a terminal,
                       even with --yes. Without it, they are kept in
                       <target-dir>.wallets-<time>.
  -h, --help           Show this help message.
EOF
}

TARGET_DIR=""
INTERACTIVE=true
ASSUME_YES=false
DELETE_WALLETS=false

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
        -y|--yes)
            ASSUME_YES=true
            shift
            ;;
        --delete-wallets)
            DELETE_WALLETS=true
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

# TARGET_DIR as the start of a path in a command line, for pgrep/pkill -f (an
# extended regex): its regex characters escaped, preceded by the start or by a
# space, '=' or a quote, and followed by '/'. So /opt/nodo does not match
# /opt/nodo2/..., nor /srv/opt/nodo/...
TARGET_RE="(^|[[:space:]=\"'])$(printf '%s' "$TARGET_DIR" | sed 's/[][\.*^$+?(){}|]/\\&/g')/"

names_target_dir() {
    # True when stdin names TARGET_DIR as a whole path: the directory itself or a
    # path under it. /opt/nodo2 and /srv/opt/nodo do not name /opt/nodo. It reads
    # all of stdin: an early exit could fail the pipeline that feeds it.
    awk -v t="$TARGET_DIR" '
        {
            s = $0
            while (!found && (i = index(s, t)) > 0) {
                b = i > 1 ? substr(s, i - 1, 1) : ""
                a = substr(s, i + length(t), 1)
                if ((b == "" || index(" \t=\"'\''", b)) && (a == "" || index("/ \t\"'\'';", a))) {
                    found = 1
                }
                s = substr(s, i + 1)
            }
        }
        END { exit !found }'
}

unit_references_target_dir() {
    systemctl show -p ExecStart -p WorkingDirectory "$1" 2>/dev/null | names_target_dir
}

unit_file_directory() {
    # The WorkingDirectory= of a unit file, to say whose it is.
    sed -n 's/^[[:space:]]*WorkingDirectory[[:space:]]*=[[:space:]]*//p' "$1" | tr -d '"' | tail -n 1
}

wrapper_directory() {
    # The directory that a wrapper install.sh wrote runs nodo from.
    sed -n 's/^cd "\(.*\)" || exit$/\1/p' "$1" | tail -n 1
}

stop_unit() {
    # A unit from plan_uninstall, which listed it. Not `list-units | grep -q`:
    # under pipefail, grep exiting at the first match can fail the pipeline.
    log "Stopping and disabling $1..."
    systemctl stop "$1" 2>/dev/null || true
    systemctl disable "$1" 2>/dev/null || true
}

stop_target_processes() {
    # stop_target_processes <what> <pattern>: TERM, then KILL what is left.
    if pgrep -f "$2" >/dev/null 2>&1; then
        log "Stopping $1..."
        pkill -f "$2" 2>/dev/null || true
        sleep 2

        if pgrep -f "$2" >/dev/null 2>&1; then
            log "Force killing $1..."
            pkill -9 -f "$2" 2>/dev/null || true
            sleep 1
        fi
    fi
}

stop_helper_processes() {
    log "Checking for processes of $TARGET_DIR..."

    # Any dockerd left over from the retired embedded Docker.
    stop_target_processes "embedded dockerd" "${TARGET_RE}bin/dockerd"

    # Stop the rootless BuildKit builder (current local-packer toolchain) and its
    # rootlesskit parent. Unlike the retired root dockerd, these belong to the
    # invoking user, so no privileged signal is needed to reach them.
    stop_target_processes "the rootless BuildKit builder" "${TARGET_RE}bin/buildkitd"

    # Stop any helper processes that may still hold mounts. Only those that run
    # from TARGET_DIR: as root, docker-init is PID 1 of every container on the
    # host that was started with --init.
    pkill -f "${TARGET_RE}docker" 2>/dev/null || true
    pkill -f "${TARGET_RE}[^[:space:]]*buildkitd-entrypoint" 2>/dev/null || true
    pkill -f "${TARGET_RE}[^[:space:]]*docker-init" 2>/dev/null || true
}

mountpoints_under_target_dir() {
    # Deepest first, so a mount is never removed from under another.
    command -v findmnt >/dev/null 2>&1 || return 0
    findmnt -rn -o TARGET | awk -v t="$TARGET_DIR" '$0 == t || index($0, t "/") == 1' | sort -r || true
}

# -----------------------------------------------------------------------------
# What belongs to TARGET_DIR, and the question
# -----------------------------------------------------------------------------

UNITS=()            # the service units that run from TARGET_DIR
OWN_UNIT=false      # SERVICE_FILE runs TARGET_DIR
OWN_ROOT_FILES=false
OWN_WRAPPER=false
WALLET_FILES=()     # config.yaml, its backups, and a copy root left in the tree
WALLET_DIR="$TARGET_DIR.wallets-$(date -u +%Y%m%d%H%M%S)"
PLAN_REMOVE=()
PLAN_KEEP=()

plan_uninstall() {
    local unit path reason

    if command -v systemctl >/dev/null 2>&1; then
        for unit in $(systemctl list-units --type=service --all --no-legend --plain 2>/dev/null | awk '{print $1}'); do
            if unit_references_target_dir "$unit"; then
                UNITS+=("$unit")
            fi
        done
    fi
    if [ "${#UNITS[@]}" -gt 0 ]; then
        PLAN_REMOVE+=("Stop and disable ${UNITS[*]} (this stops every VM it runs)")
    fi

    # The unit, and the root files of the node it runs: the service user record,
    # the root-owned virtiofsd and its AppArmor profile. They go with the unit.
    if [ -f "$SERVICE_FILE" ]; then
        if names_target_dir < "$SERVICE_FILE"; then
            OWN_UNIT=true
            OWN_ROOT_FILES=true
            PLAN_REMOVE+=("$SERVICE_FILE")
        else
            PLAN_KEEP+=("$SERVICE_FILE: it runs $(unit_file_directory "$SERVICE_FILE"), not $TARGET_DIR")
            reason="the node of $SERVICE_FILE uses it"
        fi
    else
        reason="no $SERVICE_FILE names $TARGET_DIR, so nothing shows they are its"
    fi
    for path in "$NODO_SERVICE_USER_RECORD" "$NODO_VIRTIOFSD_SYSTEM_PATH" "$NODO_VIRTIOFSD_APPARMOR_PROFILE"; do
        [ -e "$path" ] || continue
        if [ "$OWN_ROOT_FILES" = true ]; then
            PLAN_REMOVE+=("$path")
        else
            PLAN_KEEP+=("$path: $reason")
        fi
    done

    if [ -f "$WRAPPER_SCRIPT" ]; then
        if grep -Fq "\"$TARGET_DIR/nodo.py\"" "$WRAPPER_SCRIPT"; then
            OWN_WRAPPER=true
            PLAN_REMOVE+=("$WRAPPER_SCRIPT")
        else
            PLAN_KEEP+=("$WRAPPER_SCRIPT: it runs $(wrapper_directory "$WRAPPER_SCRIPT"), not $TARGET_DIR")
        fi
    fi

    if [ -d "$TARGET_DIR" ]; then
        PLAN_REMOVE+=("The processes and mounts of $TARGET_DIR, then $TARGET_DIR")
        shopt -s nullglob dotglob
        for path in "$TARGET_DIR/config.yaml" "$TARGET_DIR"/config-*.yaml "$TARGET_DIR"/.config-root.*; do
            [ -f "$path" ] && WALLET_FILES+=("$path")
        done
        shopt -u nullglob dotglob
    fi
    if [ "${#WALLET_FILES[@]}" -gt 0 ]; then
        if [ "$DELETE_WALLETS" = true ]; then
            PLAN_REMOVE+=("THE WALLET MNEMONICS: ${WALLET_FILES[*]}")
        else
            PLAN_KEEP+=("The wallet mnemonics (${#WALLET_FILES[@]} files: config.yaml and its backups): moved to $WALLET_DIR")
        fi
    fi
}

print_plan() {
    local item
    printf 'This removes the nodo install in %s.\n\nRemoved:\n' "$TARGET_DIR"
    for item in "${PLAN_REMOVE[@]}"; do
        printf '  - %s\n' "$item"
    done
    if [ "${#PLAN_KEEP[@]}" -gt 0 ]; then
        printf '\nKept:\n'
        for item in "${PLAN_KEEP[@]}"; do
            printf '  - %s\n' "$item"
        done
    fi
    printf '\n'
}

tty_available() {
    { : < /dev/tty; } 2>/dev/null
}

confirm_uninstall() {
    local answer

    plan_uninstall
    print_plan

    if [ "$ASSUME_YES" != true ]; then
        if [ "$INTERACTIVE" != true ] || ! tty_available; then
            printf "Error: nothing was removed. Run this at a terminal to answer, or add --yes.\n" >&2
            exit 1
        fi
        printf 'Remove it? [y/N] '
        read -r answer < /dev/tty || answer=""
        case "$answer" in
            y|Y|yes|YES|s|S|si|SI|sí|SÍ) ;;
            *) printf "Nothing was removed.\n"; exit 1 ;;
        esac
    fi

    # The mnemonics are the only thing here that cannot be installed again: the
    # funds on those wallets are lost without them. --yes does not answer this.
    if [ "$DELETE_WALLETS" = true ] && [ "${#WALLET_FILES[@]}" -gt 0 ]; then
        if ! tty_available; then
            printf "Error: nothing was removed. --delete-wallets asks at a terminal, even with --yes.\n" >&2
            exit 1
        fi
        printf '\nconfig.yaml holds identity.MNEMONIC and every ledgers.*.WALLET_MNEMONIC.\n'
        printf 'Without a copy of them, the funds on those wallets are lost.\n'
        printf 'Type "delete wallets" to delete them: '
        read -r answer < /dev/tty || answer=""
        if [ "$answer" != "delete wallets" ]; then
            printf "Nothing was removed.\n"
            exit 1
        fi
    fi
}

keep_wallets() {
    # Before the rm -rf: config.yaml and its backups out of the tree.
    [ "${#WALLET_FILES[@]}" -gt 0 ] || return 0
    if [ "$DELETE_WALLETS" = true ]; then
        log "Deleting the wallet mnemonics (--delete-wallets)..."
        return 0
    fi
    if ! mkdir -m 0700 -- "$WALLET_DIR" || ! mv -- "${WALLET_FILES[@]}" "$WALLET_DIR/"; then
        printf "Error: could not move the wallet mnemonics to %s. %s was not removed.\n" \
            "$WALLET_DIR" "$TARGET_DIR" >&2
        exit 1
    fi
    log "Kept the wallet mnemonics (config.yaml and its backups) in $WALLET_DIR."
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
    if [ "$OWN_WRAPPER" = true ] && [ -f "$WRAPPER_SCRIPT" ]; then
        log "Removing wrapper script $WRAPPER_SCRIPT..."
        rm -f "$WRAPPER_SCRIPT"
    fi
}

# -----------------------------------------------------------------------------
# Without sudo
# -----------------------------------------------------------------------------

uninstall_unprivileged() {
    local privileged_script="$TARGET_DIR/uninstall-privileged.sh"
    local mountpoint

    NODO_PRIV_FILE="$(mktemp)"
    export NODO_PRIV_FILE
    trap 'rm -f "$NODO_PRIV_FILE"' EXIT

    # The user's own processes first: they can hold mounts under the target.
    stop_helper_processes

    # Each step is recorded only when it is needed, so a second run (after the
    # operator ran the saved script) finds nothing left to ask for. Only what
    # plan_uninstall found to be this install's: a nodo.service that runs another
    # directory, and the root files of its node, are not.
    if [ "${#UNITS[@]}" -gt 0 ]; then
        priv_record "Stop and disable ${UNITS[*]} (this stops every VM it runs)" \
            "sudo systemctl disable --now ${UNITS[*]}"
    fi

    if [ "$OWN_UNIT" = true ]; then
        priv_record "Remove the systemd unit" \
            "sudo rm -f $(printf '%q' "$SERVICE_FILE") && sudo systemctl daemon-reload"
    fi

    if [ "$OWN_ROOT_FILES" = true ] && [ -e "$NODO_SERVICE_USER_RECORD" ]; then
        priv_record "Remove the record of the user nodo runs as" \
            "sudo rm -f $(printf '%q' "$NODO_SERVICE_USER_RECORD") && sudo rmdir --ignore-fail-on-non-empty $(printf '%q' "$(dirname "$NODO_SERVICE_USER_RECORD")")"
    fi

    if [ "$OWN_ROOT_FILES" = true ] && [ -e "$NODO_VIRTIOFSD_APPARMOR_PROFILE" ]; then
        priv_record "Unload and remove the AppArmor profile for virtiofsd" \
            "{ sudo apparmor_parser -R $(printf '%q' "$NODO_VIRTIOFSD_APPARMOR_PROFILE") || true; } && sudo rm -f $(printf '%q' "$NODO_VIRTIOFSD_APPARMOR_PROFILE")"
    fi
    if [ "$OWN_ROOT_FILES" = true ] && [ -e "$NODO_VIRTIOFSD_SYSTEM_PATH" ]; then
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

    # What the user owns. The completion is the same for every install of this
    # user, so it stays while the wrapper of another one does.
    remove_wrapper
    if [ "$OWN_WRAPPER" = true ] || [ ! -e "$WRAPPER_SCRIPT" ]; then
        remove_user_completion
    fi

    if [ -d "$TARGET_DIR" ]; then
        keep_wallets
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
    local unit

    # Stop the services of this install
    for unit in "${UNITS[@]}"; do
        stop_unit "$unit"
    done

    # Cleanup legacy embedded Docker installations
    stop_helper_processes
    unmount_target_dir

    # Remove systemd service, and the root files of its node
    if [ "$OWN_UNIT" = true ]; then
        log "Removing service file $SERVICE_FILE..."
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
    fi
    if [ "$OWN_ROOT_FILES" = true ]; then
        remove_root_files
    fi
    remove_wrapper

    # Remove installation directory
    if [ -d "$TARGET_DIR" ]; then
        keep_wallets
        log "Removing project directory $TARGET_DIR..."
        cd /
        rm -rf "$TARGET_DIR"
    fi

    log "Uninstallation completed successfully."
}

confirm_uninstall

if [ "$UNPRIVILEGED" = true ]; then
    uninstall_unprivileged
else
    uninstall_privileged
fi
