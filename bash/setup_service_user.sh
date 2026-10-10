#!/bin/bash
# Run the nodo daemon as a dedicated system user instead of root (PR #466,
# phase 2). Opt-in: `sudo ./install.sh --service-user nodo` calls this, and it
# can be run on its own against an existing install.
#
# What it does, each step idempotent:
#   1. creates the system user (and its group), adds it to group kvm;
#   2. gives it a /etc/subuid and /etc/subgid range, for virtiofsd's namespace
#      sandbox (chroot is root only);
#   3. writes the keys a non-root daemon needs into config.yaml: main.SERVICE_USER,
#      virtualizers.ch.CGROUPS_BASE_DIR (the unit's delegated cgroup) and
#      virtualizers.ch.API_SOCKET_DIR (under the unit's RuntimeDirectory);
#   4. gives the storage tree and config.yaml to that user (config.yaml 0660), and
#      adds the operator to its group so they can still edit the config;
#   5. renders bash/nodo-nosudo.service.template into /etc/systemd/system/<unit>.service.
#
# It does not start or enable the unit; the caller does. To go back to root, set
# main.SERVICE_USER to "" in config.yaml and run install.sh again: it renders the
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
[ -n "$SERVICE_USER" ] || { usage >&2; fail "--user is required."; }
[ "$SERVICE_USER" != "root" ] || fail "--user root is the default mode; run install.sh without --service-user."
printf '%s' "$SERVICE_USER" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$' || fail "invalid user name: $SERVICE_USER"
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
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  printf 'Creating system user %s...\n' "$SERVICE_USER"
  useradd --system --user-group --home-dir "$TARGET_DIR" --no-create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -aG kvm "$SERVICE_USER"

# --- 2. subordinate ids, for virtiofsd --sandbox namespace --------------------
# Same allocation as bash/install_buildkit.sh: a 65536-wide range past every
# range already allocated, because usermod does not check for overlaps.
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
# shellcheck source=bash/lib_pkg.sh
. "$TARGET_DIR/bash/lib_pkg.sh"
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

# --- 3. config keys ---------------------------------------------------------
STORAGE_DIR="$(config_value '.main.STORAGE' "$TARGET_DIR/storage")"
"$YQ" -i ".main.SERVICE_USER = \"$SERVICE_USER\"" "$CONFIG_FILE"
"$YQ" -i ".virtualizers.ch.CGROUPS_BASE_DIR = \"/sys/fs/cgroup/system.slice/$UNIT.service\"" "$CONFIG_FILE"
"$YQ" -i ".virtualizers.ch.API_SOCKET_DIR = \"/run/$UNIT/ch\"" "$CONFIG_FILE"

# --- 4. ownership -------------------------------------------------------------
mkdir -p "$STORAGE_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$STORAGE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
chmod 0660 "$CONFIG_FILE"
for backup in "$TARGET_DIR"/config-*.yaml; do
  [ -e "$backup" ] || continue
  chown "$SERVICE_USER:$SERVICE_USER" "$backup"
  chmod 0660 "$backup"
done
if [ -n "$OPERATOR" ] && [ "$OPERATOR" != "root" ]; then
  usermod -aG "$SERVICE_USER" "$OPERATOR"
  printf 'Added %s to group %s (log in again for it to apply).\n' "$OPERATOR" "$SERVICE_USER"
fi

# --- 5. the unit ----------------------------------------------------------------
[ -n "$JAVA_HOME_PATH" ] || JAVA_HOME_PATH="$(config_value '.dependencies.java.JAVA_HOME' "$TARGET_DIR/runtime/java/current")"
if [ -z "$PYTHON_RUNTIME_BIN_DIR_PATH" ]; then
  PYTHON_RUNTIME_BIN_DIR_PATH="$(dirname "$(config_value '.dependencies.python.RUNTIME_BIN' "$TARGET_DIR/runtime/python/current/bin/python3")")"
fi
[ -n "$PYTHON_VENV_BIN_PATH" ] || PYTHON_VENV_BIN_PATH="$(config_value '.dependencies.python.VENV_BIN' "$TARGET_DIR/venv/bin/python")"

escape_for_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
rendered="$(mktemp)"
sed \
  -e "s|{{MAIN_DIR}}|$(escape_for_sed "$TARGET_DIR")|g" \
  -e "s|{{JAVA_HOME}}|$(escape_for_sed "$JAVA_HOME_PATH")|g" \
  -e "s|{{PYTHON_RUNTIME_BIN_DIR}}|$(escape_for_sed "$PYTHON_RUNTIME_BIN_DIR_PATH")|g" \
  -e "s|{{PYTHON_VENV_BIN}}|$(escape_for_sed "$PYTHON_VENV_BIN_PATH")|g" \
  -e "s|{{SERVICE_USER}}|$SERVICE_USER|g" \
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
