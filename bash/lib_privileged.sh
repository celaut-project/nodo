# The steps of an install that need root, for an install that runs without it.
# Sourced, not executed.
#
# `./install.sh` without sudo does everything that does not need root itself and
# collects the rest here, each as the exact command that does it. At the end it
# prints them, saves them to $TARGET_DIR/install-privileged.sh, and asks before it
# runs them with sudo: the operator reads every command before anything runs as
# root, and can run them later by hand instead.
#
# The list is a file (NODO_PRIV_FILE), not a shell array, because the setup
# scripts run as child processes and add to it too. One record per line:
# <description> TAB <command>.

if ! declare -F fail >/dev/null 2>&1; then
    fail() {
        echo "Error: $1" >&2
        exit 1
    }
fi

# Where an unprivileged node keeps the virtiofsd that its AppArmor profile names.
# Owned by root on purpose: the profile lets that binary create a user namespace
# on hosts that restrict them (Ubuntu 23.10+), and a binary the daemon's user can
# replace would carry that grant to whatever it is replaced with. The path is
# fixed rather than read from config.yaml for the same reason: config.yaml is
# writable by that user, so it could name /usr/bin/unshare.
NODO_VIRTIOFSD_SYSTEM_PATH="/usr/local/libexec/nodo/virtiofsd"
NODO_VIRTIOFSD_APPARMOR_PROFILE="/etc/apparmor.d/nodo-virtiofsd"

# The user the daemon runs as, when it is not root. Root's file, and the only
# place a step that runs as root reads it from: config.yaml is writable by that
# user, and a value there that says "root" would make the next `sudo nodo update`
# or `sudo nodo doctor` install a root unit for it.
NODO_SERVICE_USER_RECORD="/etc/nodo/service-user"

valid_user_name() {
    local pattern='^[A-Za-z_][A-Za-z0-9._-]{0,31}$'
    [[ "$1" =~ $pattern ]]
}

require_user_name() {
    # require_user_name <what> <value>
    valid_user_name "$2" || fail "$1 is not a valid user or group name: $(printf '%q' "$2")."
}

# A path that goes into a systemd unit. Only characters that neither systemd nor
# the unit's `bash -c` give a meaning to: no newline (a new directive), no
# backslash (an escape, or a line continuation), no quote, '$' or '`' (the
# shell), no '%' (a systemd specifier), no ':' (the PATH separator) and no ';'.
# The same rule as _UNIT_PATH in src/commands/doctor.py.
valid_unit_path() {
    local pattern='^/[A-Za-z0-9 ._+@,/-]*$'
    [[ "$1" =~ $pattern ]]
}

require_unit_paths() {
    # require_unit_paths "<what>=<value>"...: fails on the first value that
    # cannot go into a unit, and names it.
    local pair
    for pair in "$@"; do
        valid_unit_path "${pair#*=}" || fail "${pair%%=*} cannot go into the systemd unit: $(printf '%q' "${pair#*=}").
Use an absolute path made only of letters, digits, spaces and . _ + @ , / -"
    done
}

read_service_user_record() {
    # The user in NODO_SERVICE_USER_RECORD, or nothing when there is no record.
    # Fails when the record is not root's alone, or does not hold one user name.
    local file="$NODO_SERVICE_USER_RECORD" path owner mode name
    [ -e "$file" ] || return 0
    for path in "$file" "$(dirname "$file")"; do
        read -r owner mode < <(stat -c '%u %a' -- "$path")
        [ "$owner" = 0 ] && (( (8#$mode & 8#022) == 0 )) \
            || fail "$path must be owned by root and writable only by root."
    done
    name="$(cat -- "$file")"
    require_user_name "$file" "$name"
    printf '%s' "$name"
}

write_service_user_record() {
    # write_service_user_record <user>: an empty user removes the record (root).
    if [ -z "$1" ]; then
        rm -f -- "$NODO_SERVICE_USER_RECORD"
        return 0
    fi
    require_user_name "service user" "$1"
    install -d -o root -g root -m 0755 "$(dirname "$NODO_SERVICE_USER_RECORD")"
    printf '%s' "$1" > "$NODO_SERVICE_USER_RECORD.tmp"
    chown root:root "$NODO_SERVICE_USER_RECORD.tmp"
    chmod 0644 "$NODO_SERVICE_USER_RECORD.tmp"
    mv -f "$NODO_SERVICE_USER_RECORD.tmp" "$NODO_SERVICE_USER_RECORD"
}

unit_user() {
    # unit_user <unit file>: its last User=, or nothing when it has none.
    [ -f "$1" ] || return 0
    sed -n 's/^[[:space:]]*User[[:space:]]*=[[:space:]]*\([^[:space:]]*\)[[:space:]]*$/\1/p' "$1" | tail -n 1
}

installed_service_user() {
    # The user the daemon runs as, from root's sources only: the record, else the
    # User= of the installed unit (a node set up before the record existed).
    # Nothing means root.
    local name
    name="$(read_service_user_record)" || exit 1
    [ -n "$name" ] || name="$(unit_user "${1:-/etc/systemd/system/nodo.service}")"
    [ "$name" != root ] || name=""
    [ -z "$name" ] || require_user_name "the User= of the nodo unit" "$name"
    printf '%s' "$name"
}

priv_record() {
    # priv_record <description> <command>
    local file="${NODO_PRIV_FILE:-}"
    [ -n "$file" ] || fail "priv_record: NODO_PRIV_FILE is not set."
    case "$1$2" in
        *$'\t'*|*$'\n'*) fail "priv_record: a description or command holds a tab or a newline." ;;
    esac
    printf '%s\t%s\n' "$1" "$2" >> "$file"
}

priv_pending() {
    [ -n "${NODO_PRIV_FILE:-}" ] && [ -s "$NODO_PRIV_FILE" ]
}

priv_print() {
    local description command index=0
    while IFS=$'\t' read -r description command; do
        index=$((index + 1))
        printf '  %d. %s\n     %s\n' "$index" "$description" "$command"
    done < "$NODO_PRIV_FILE"
}

priv_save_script() {
    # priv_save_script <path>: the same commands as a script, to run or read later.
    local path="$1" description command
    {
        printf '#!/bin/bash\n'
        printf '# Written by nodo install.sh: the steps it could not take without root.\n'
        printf '# Read it, then run it: bash %s\n' "$path"
        printf 'set -euo pipefail\n'
        while IFS=$'\t' read -r description command; do
            printf '\n# %s\n%s\n' "$description" "$command"
        done < "$NODO_PRIV_FILE"
    } > "$path"
    chmod 0700 "$path"
}

priv_offer() {
    # priv_offer <title> <interactive:true|false> [script path]
    #
    # Prints the pending commands under <title> and, when <interactive> is true and
    # a terminal is there to answer, asks before running them one by one. Returns
    # 0 when every command ran (the list is then emptied), 1 when they were not
    # run, and 2 when one failed (the list is kept).
    local title="$1" interactive="$2" script="${3:-}" answer description command

    priv_pending || return 0

    printf '\n%s\n\n' "$title"
    priv_print
    if [ -n "$script" ]; then
        priv_save_script "$script"
        printf '\nSaved to %s.\n' "$script"
    fi

    if [ "$interactive" != true ] || ! { : < /dev/tty; } 2>/dev/null; then
        return 1
    fi
    printf '\nRun them now with sudo? [y/N] '
    read -r answer < /dev/tty || answer=""
    case "$answer" in
        y|Y|yes|YES|s|S|si|SI|sí|SÍ) ;;
        *) return 1 ;;
    esac

    while IFS=$'\t' read -r description command <&3; do
        printf '\n+ %s\n' "$command"
        if ! bash -c "$command" < /dev/tty; then
            printf 'Error: that command failed. Nothing after it was run.\n' >&2
            return 2
        fi
    done 3< "$NODO_PRIV_FILE"
    : > "$NODO_PRIV_FILE"
    [ -z "$script" ] || rm -f "$script"
    return 0
}

# A 65536-wide subordinate id range past every range already allocated in <file>,
# because usermod does not check for overlaps. Same allocation as
# bash/install_buildkit.sh.
next_free_subid_start() {
    local file="$1" size=65536 start=100000
    [ -r "$file" ] || { printf '%s' "$start"; return 0; }
    awk -F: -v start="$start" -v size="$size" '
        BEGIN { n = 0 }
        NF >= 3 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { s[n]=$2; c[n]=$3; n++ }
        END {
            for (i = 1; i < n; i++) {
                ks = s[i]; kc = c[i]; j = i - 1
                while (j >= 0 && s[j] > ks) { s[j+1]=s[j]; c[j+1]=c[j]; j-- }
                s[j+1]=ks; c[j+1]=kc
            }
            for (i = 0; i < n; i++) {
                if (start + size - 1 < s[i]) break
                if (start < s[i] + c[i]) start = s[i] + c[i]
            }
            print start
        }' "$file"
}

has_subids() {
    # has_subids <user>: a /etc/subuid and a /etc/subgid range for <user>.
    grep -q "^$1:" /etc/subuid 2>/dev/null && grep -q "^$1:" /etc/subgid 2>/dev/null
}

apparmor_restricts_userns() {
    [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ]
}

render_virtiofsd_apparmor_profile() {
    # The profile that lets NODO_VIRTIOFSD_SYSTEM_PATH, and nothing else, create a
    # user namespace. The fix Ubuntu itself uses for crun, podman and lxc.
    cat <<PROFILE
# Installed by nodo: lets virtiofsd --sandbox namespace create its user namespace
# under kernel.apparmor_restrict_unprivileged_userns.
abi <abi/4.0>,
include <tunables/global>

profile nodo-virtiofsd ${NODO_VIRTIOFSD_SYSTEM_PATH} flags=(unconfined) {
  userns,

  include if exists <local/nodo-virtiofsd>
}
PROFILE
}
