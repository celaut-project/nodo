#!/bin/bash
# Run the nodo daemon as a dedicated system user instead of root (PR #466,
# phase 2). Opt-in: `sudo ./install.sh --service-user nodo` calls this, and it
# can be run on its own against an existing install.
#
# What it does, each step idempotent:
#   1. creates the system user (and its group, home /var/lib/<user>), adds it
#      to group kvm;
#   2. gives it a /etc/subuid and /etc/subgid range, for virtiofsd's namespace
#      sandbox (chroot is root only), installs uidmap and squashfs-tools, and on
#      hosts that restrict unprivileged user namespaces with AppArmor, a
#      root-owned copy of virtiofsd and a profile that lets that copy create one;
#   3. records the user in /etc/nodo/service-user (root's file: config.yaml is
#      the user's to write, so no step that runs as root reads it from there), and
#      writes the keys a non-root daemon needs into config.yaml:
#      virtualizers.ch.CGROUPS_BASE_DIR (the unit's delegated cgroup) and
#      virtualizers.ch.API_SOCKET_DIR (under the unit's RuntimeDirectory);
#   4. gives the storage tree, the local packer's buildkit/ directory and
#      config.yaml to that user (config.yaml 0660), and adds the operator to its
#      group so they can still edit the config;
#   5. renders bash/nodo-nosudo.service.template into /etc/systemd/system/<unit>.service.
#
# It does not start or enable the unit; the caller does. To go back to root, run
# `sudo ./install.sh --service-user root`: it removes the record and renders the
# root unit again.

set -euo pipefail

TARGET_DIR="/nodo"
SERVICE_USER=""
UNIT="nodo"
OPERATOR=""
JAVA_HOME_PATH=""
PYTHON_RUNTIME_BIN_DIR_PATH=""
PYTHON_VENV_BIN_PATH=""

usage() {
  cat <<EOF
Usage: sudo bash setup_service_user.sh --user <name> [options]

  --user <name>                   System user the daemon runs as (created if missing).
  --target-dir <path>             nodo install directory (default: /nodo).
  --unit <name>                   systemd unit name without .service (default: nodo).
  --operator <login>              Login user to add to the service user's group.
  --java-home <path>              JAVA_HOME for the unit (default: from config.yaml).
  --python-runtime-bin-dir <dir>  Python runtime bin dir (default: from config.yaml).
  --python-venv-bin <path>        venv python (default: from config.yaml).
EOF
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --target-dir) TARGET_DIR="${2:-}"; shift 2 ;;
    --unit) UNIT="${2:-}"; shift 2 ;;
    --operator) OPERATOR="${2:-}"; shift 2 ;;
    --java-home) JAVA_HOME_PATH="${2:-}"; shift 2 ;;
    --python-runtime-bin-dir) PYTHON_RUNTIME_BIN_DIR_PATH="${2:-}"; shift 2 ;;
    --python-venv-bin) PYTHON_VENV_BIN_PATH="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || fail "run this with sudo."
[ -f "$TARGET_DIR/bash/lib_privileged.sh" ] || fail "$TARGET_DIR is not a nodo install."
# shellcheck source=bash/lib_pkg.sh
. "$TARGET_DIR/bash/lib_pkg.sh"
# shellcheck source=bash/lib_privileged.sh
. "$TARGET_DIR/bash/lib_privileged.sh"

[ -n "$SERVICE_USER" ] || { usage >&2; fail "--user is required."; }
[ "$SERVICE_USER" != "root" ] || fail "--user root is the default mode; run install.sh without --service-user."
require_user_name "--user" "$SERVICE_USER"
printf '%s' "$UNIT" | grep -Eq '^[A-Za-z0-9_-]{1,64}$' || fail "invalid unit name: $UNIT"
if [ -n "$OPERATOR" ] && [ "$OPERATOR" != "root" ] && ! id "$OPERATOR" >/dev/null 2>&1; then
  fail "no such operator user: $OPERATOR"
fi

CONFIG_FILE="$TARGET_DIR/config.yaml"
TEMPLATE="$TARGET_DIR/bash/nodo-nosudo.service.template"
YQ="$TARGET_DIR/bin/yq"
SERVICE_FILE="/etc/systemd/system/$UNIT.service"
[ -f "$CONFIG_FILE" ] || fail "$CONFIG_FILE not found."
[ -f "$TEMPLATE" ] || fail "$TEMPLATE not found."
[ -x "$YQ" ] || fail "$YQ not found; run install.sh first."
getent group kvm >/dev/null || fail "group kvm does not exist; KVM is not set up on this host."

expand_main_dir() { printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"; }
config_value() {
  local value
  value="$("$YQ" -r "$1 // \"\"" "$CONFIG_FILE" 2>/dev/null || true)"
  [ -n "$value" ] && [ "$value" != "null" ] || value="$2"
  expand_main_dir "$value"
}

# --- 1. the user ------------------------------------------------------------
# A system user gets a home of its own: the local packer's buildctl writes
# ~/.docker. A login user (an install made without sudo runs the daemon as the
# operator, and `sudo nodo update` comes back here) keeps the home it has: it is
# theirs, not this script's, to chown or chmod.
SYSTEM_HOME="/var/lib/$SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  printf 'Creating system user %s...\n' "$SERVICE_USER"
  useradd --system --user-group --home-dir "$SYSTEM_HOME" --no-create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
# The user's primary group: the one useradd made for a system user, and whatever
# a login user has (not always a group of the same name).
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
if [ "$SERVICE_HOME" = "$SYSTEM_HOME" ]; then
  mkdir -p "$SERVICE_HOME"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$SERVICE_HOME"
  chmod 0750 "$SERVICE_HOME"
elif [ -z "$SERVICE_HOME" ] || [ "$SERVICE_HOME" = "/" ] || [ ! -d "$SERVICE_HOME" ]; then
  printf 'Warning: the home of %s is %s; nodo pack --local needs a home it can write.\n' \
    "$SERVICE_USER" "${SERVICE_HOME:-<none>}" >&2
fi
usermod -aG kvm "$SERVICE_USER"

# --- 2. subordinate ids, for virtiofsd --sandbox namespace --------------------
for kind in uid gid; do
  file="/etc/sub$kind"
  if ! grep -q "^$SERVICE_USER:" "$file" 2>/dev/null; then
    start="$(next_free_subid_start "$file")"
    printf 'Allocating sub%ss %s-%s to %s...\n' "$kind" "$start" "$((start + 65535))" "$SERVICE_USER"
    usermod "--add-sub${kind}s" "$start-$((start + 65535))" "$SERVICE_USER"
  fi
done

# newuidmap/newgidmap for that sandbox, and mksquashfs for read-only rootfs
# images: mkfs.erofs cannot carry the owners of a tree staged without root.
missing=()
command -v newuidmap >/dev/null && command -v newgidmap >/dev/null || missing+=(uidmap)
command -v mksquashfs >/dev/null || missing+=(squashfs-tools)
if [ "${#missing[@]}" -gt 0 ]; then
  detect_pkg_mgr
  packages=()
  for name in "${missing[@]}"; do
    case "$PKG_MGR:$name" in
      dnf:uidmap) packages+=(shadow-utils) ;;
      *) packages+=("$name") ;;
    esac
  done
  printf 'Installing %s...\n' "${packages[*]}"
  case "$PKG_MGR" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}" ;;
    dnf) dnf install -y "${packages[@]}" ;;
  esac || fail "could not install ${packages[*]}."
fi

# Ubuntu 23.10+ confines a process that creates a user namespace without
# privilege (kernel.apparmor_restrict_unprivileged_userns=1), and virtiofsd then
# fails "Error entering sandbox: CleanMount(... Permission denied)". The fix
# Ubuntu itself uses for crun, podman and lxc: a profile that allows userns for
# that one binary and confines nothing else.
#
# The profile names a root-owned copy at a fixed path, never the path in
# config.yaml: the service user can write config.yaml, and a profile that follows
# it would grant userns to whatever binary it names (/usr/bin/unshare, or a glob).
VIRTIOFSD_SOURCE="$TARGET_DIR/bin/virtiofsd"
if apparmor_restricts_userns; then
  if [ -x "$VIRTIOFSD_SOURCE" ] && command -v apparmor_parser >/dev/null; then
    printf 'Allowing user namespaces for %s (%s)...\n' "$NODO_VIRTIOFSD_SYSTEM_PATH" "$NODO_VIRTIOFSD_APPARMOR_PROFILE"
    install -D -o root -g root -m 0755 "$VIRTIOFSD_SOURCE" "$NODO_VIRTIOFSD_SYSTEM_PATH"
    render_virtiofsd_apparmor_profile > "$NODO_VIRTIOFSD_APPARMOR_PROFILE"
    apparmor_parser -r "$NODO_VIRTIOFSD_APPARMOR_PROFILE" || fail "could not load $NODO_VIRTIOFSD_APPARMOR_PROFILE."
    VIRTIOFSD_PATH="$NODO_VIRTIOFSD_SYSTEM_PATH" "$YQ" -i \
      '.virtualizers.ch.VIRTIOFSD_BINARY = strenv(VIRTIOFSD_PATH)' "$CONFIG_FILE"
  else
    printf 'Warning: unprivileged user namespaces are restricted by AppArmor and %s or apparmor_parser was not found: services with shared directories will not start.\n' "$VIRTIOFSD_SOURCE" >&2
  fi
fi

# --- 3. config keys ---------------------------------------------------------
STORAGE_DIR="$(config_value '.main.STORAGE' "$TARGET_DIR/storage")"
write_service_user_record "$SERVICE_USER"
"$YQ" -i ".virtualizers.ch.CGROUPS_BASE_DIR = \"/sys/fs/cgroup/system.slice/$UNIT.service\"" "$CONFIG_FILE"
"$YQ" -i ".virtualizers.ch.API_SOCKET_DIR = \"/run/$UNIT/ch\"" "$CONFIG_FILE"

# --- 4. ownership -------------------------------------------------------------
# The storage tree is given to the user with a chown -R that runs as root, and
# main.STORAGE comes from config.yaml, which that user can write: see
# service_storage_dir in bash/lib_privileged.sh.
STORAGE_REAL="$(service_storage_dir "$TARGET_DIR" "$STORAGE_DIR" "$SERVICE_USER" "$OPERATOR")" || exit 1
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$STORAGE_REAL"
# The rootless BuildKit builder of `nodo pack --local` keeps its state here. Packing
# writes the registry, which is now the service user's, so it packs as that user.
mkdir -p "$TARGET_DIR/buildkit"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$TARGET_DIR/buildkit"
chown "$SERVICE_USER:$SERVICE_GROUP" "$CONFIG_FILE"
chmod 0660 "$CONFIG_FILE"
for backup in "$TARGET_DIR"/config-*.yaml; do
  [ -e "$backup" ] || continue
  chown "$SERVICE_USER:$SERVICE_GROUP" "$backup"
  chmod 0660 "$backup"
done
if [ -n "$OPERATOR" ] && [ "$OPERATOR" != "root" ] && [ "$OPERATOR" != "$SERVICE_USER" ]; then
  usermod -aG "$SERVICE_GROUP" "$OPERATOR"
  printf 'Added %s to group %s (log in again for it to apply).\n' "$OPERATOR" "$SERVICE_GROUP"
fi

# --- 5. the unit ----------------------------------------------------------------
[ -n "$JAVA_HOME_PATH" ] || JAVA_HOME_PATH="$(config_value '.dependencies.java.JAVA_HOME' "$TARGET_DIR/runtime/java/current")"
if [ -z "$PYTHON_RUNTIME_BIN_DIR_PATH" ]; then
  PYTHON_RUNTIME_BIN_DIR_PATH="$(dirname "$(config_value '.dependencies.python.RUNTIME_BIN' "$TARGET_DIR/runtime/python/current/bin/python3")")"
fi
[ -n "$PYTHON_VENV_BIN_PATH" ] || PYTHON_VENV_BIN_PATH="$(config_value '.dependencies.python.VENV_BIN' "$TARGET_DIR/venv/bin/python")"

# Every value that goes into the unit, checked: the defaults come from
# config.yaml, which the service user can write. A newline there would add a
# directive (a second User=), and a backslash would continue a line.
require_unit_paths "the install directory=$TARGET_DIR" \
  "dependencies.java.JAVA_HOME=$JAVA_HOME_PATH" \
  "dependencies.python.RUNTIME_BIN=$PYTHON_RUNTIME_BIN_DIR_PATH" \
  "dependencies.python.VENV_BIN=$PYTHON_VENV_BIN_PATH"
require_user_name "the service group" "$SERVICE_GROUP"
escape_for_sed() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }
rendered="$(mktemp)"
sed \
  -e "s|{{MAIN_DIR}}|$(escape_for_sed "$TARGET_DIR")|g" \
  -e "s|{{JAVA_HOME}}|$(escape_for_sed "$JAVA_HOME_PATH")|g" \
  -e "s|{{PYTHON_RUNTIME_BIN_DIR}}|$(escape_for_sed "$PYTHON_RUNTIME_BIN_DIR_PATH")|g" \
  -e "s|{{PYTHON_VENV_BIN}}|$(escape_for_sed "$PYTHON_VENV_BIN_PATH")|g" \
  -e "s|{{SERVICE_USER}}|$SERVICE_USER|g" \
  -e "s|{{SERVICE_GROUP}}|$SERVICE_GROUP|g" \
  -e "s|{{RUNTIME_DIR_NAME}}|$UNIT|g" \
  "$TEMPLATE" > "$rendered"
if grep -q '{{[A-Z_][A-Z_]*}}' "$rendered"; then
  grep -o '{{[A-Z_][A-Z_]*}}' "$rendered" | sort -u >&2
  rm -f "$rendered"
  fail "unresolved placeholders in the generated unit."
fi
install -m 0644 "$rendered" "$SERVICE_FILE"
rm -f "$rendered"
systemctl daemon-reload
printf '%s runs as %s with CAP_NET_ADMIN only. Start it with: systemctl restart %s.service\n' \
  "$SERVICE_FILE" "$SERVICE_USER" "$UNIT"
