#!/bin/bash

if [ -z "${BASH_VERSION:-}" ]; then
  printf "Error: This installer requires bash. Run: bash install.sh\n" >&2
  exit 1
fi

# Without sudo, the install does everything that does not need root itself, and
# collects the rest as exact commands (bash/lib_privileged.sh): it prints them,
# saves them, and asks before it runs them with sudo. The daemon then runs as the
# installing user, with CAP_NET_ADMIN only (bash/nodo-nosudo.service.template).
if [ "$(id -u)" -eq 0 ]; then
  UNPRIVILEGED=false
else
  UNPRIVILEGED=true
  # Debian leaves the sbin directories out of an ordinary user's PATH, and
  # iptables, debugfs and mkfs.ext4 live there: without them the checks below
  # would ask for packages the host already has.
  PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
  export PATH
fi

# Defaults
REPO_URL="https://github.com/celaut-project/nodo.git"
TARGET_DIR=""
INTERACTIVE=true
SERVICE_FILE="/etc/systemd/system/nodo.service"
MAX_RETRIES=3
USE_LOCAL_SOURCE=false
BRANCH="stable"
BRANCH_EXPLICIT=false
CH_VERSION="v51.1"
# Guest kernel + busybox published by .github/workflows/guest-kernel.yml; bumped independently
# of nodo releases so a kernel fix does not require cutting a node release.
GUEST_KERNEL_VERSION="guest-kernel"
SERVICE_USER=""

print_usage() {
  cat <<EOF
Usage: ./install.sh [options]        install as you; asks for the root steps
       sudo ./install.sh [options]   install as root (the daemon runs as root)

Without sudo, everything that does not need root is done as you, and the steps
that do (missing packages, the systemd unit) are printed as exact commands,
saved to <target-dir>/install-privileged.sh, and run with sudo only if you say
so. The daemon then runs as you, with CAP_NET_ADMIN only.

Options:
  --source-dir <path>  Use an existing local source directory instead of cloning.
  --target-dir <path>  Install/clone into this directory (default: /nodo with
                       sudo, ~/.local/share/nodo without).
  --repo-url <url>     Repository URL for clone/pull.
  --branch <name>      Git branch for clone/pull (default: stable).
  --service-user <name>
                       Run the daemon as this system user, with CAP_NET_ADMIN
                       only, instead of root (created if missing). Opt-in; see
                       docs/ROOTLESS.md. Needs sudo. 'nodo update' keeps it;
                       --service-user root goes back to a root daemon.
  --no-input           Without sudo: print the root steps, do not offer to run them.
  -h, --help           Show this help message.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --source-dir)
      if [ -z "$2" ]; then
        printf "Error: --source-dir requires a path.\n" >&2
        exit 1
      fi
      TARGET_DIR="$2"
      USE_LOCAL_SOURCE=true
      shift 2
      ;;
    --target-dir)
      if [ -z "$2" ]; then
        printf "Error: --target-dir requires a path.\n" >&2
        exit 1
      fi
      TARGET_DIR="$2"
      shift 2
      ;;
    --repo-url)
      if [ -z "$2" ]; then
        printf "Error: --repo-url requires a URL.\n" >&2
        exit 1
      fi
      REPO_URL="$2"
      shift 2
      ;;
    --branch)
      if [ -z "$2" ]; then
        printf "Error: --branch requires a branch name.\n" >&2
        exit 1
      fi
      BRANCH="$2"
      BRANCH_EXPLICIT=true
      shift 2
      ;;
    --service-user)
      if [ -z "$2" ]; then
        printf "Error: --service-user requires a user name.\n" >&2
        exit 1
      fi
      SERVICE_USER="$2"
      shift 2
      ;;
    --no-input)
      INTERACTIVE=false
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      printf "Error: Unknown argument '%s'.\n" "$1" >&2
      print_usage
      exit 1
      ;;
  esac
done

if [ -z "$TARGET_DIR" ]; then
  if [ "$UNPRIVILEGED" = true ]; then
    TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nodo"
  else
    TARGET_DIR="/nodo"
  fi
fi

if [ "$UNPRIVILEGED" = true ] && [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != "$(id -un)" ]; then
  printf "Error: --service-user creates a system user and needs sudo. Without sudo the daemon runs as you (%s).\n" "$(id -un)" >&2
  exit 1
fi

# Make TARGET_DIR absolute unconditionally, before the directory necessarily
# exists (so this can't rely on `cd`+`pwd`). setup_linux_arm.sh/setup_linux_x86.sh
# cd into TARGET_DIR themselves now, and a relative one would resolve against
# their own cwd (already TARGET_DIR, see below) instead of the caller's --
# doubling the path.
case "$TARGET_DIR" in
  /*) ;;
  *) TARGET_DIR="$(pwd)/$TARGET_DIR" ;;
esac

# Normalize local source path to absolute
if [ "$USE_LOCAL_SOURCE" = true ]; then
  if [ ! -d "$TARGET_DIR" ]; then
    printf "Error: Local source directory '%s' does not exist.\n" "$TARGET_DIR" >&2
    exit 1
  fi
  TARGET_DIR="$(cd "$TARGET_DIR" >/dev/null 2>&1 && pwd)"
fi

# Function to install git if it's not already installed
install_git_if_needed() {
  # Before the checkout, so bash/lib_privileged.sh is not there yet: the one
  # command is asked for here.
  if ! command -v git >/dev/null 2>&1 && [ "$UNPRIVILEGED" = true ]; then
    local command=""
    if command -v apt-get >/dev/null 2>&1; then
      command="sudo apt-get update && sudo apt-get install -y git"
    elif command -v dnf >/dev/null 2>&1; then
      command="sudo dnf install -y git"
    fi
    printf "Git is not installed, and installing it needs root:\n\n  %s\n\n" "${command:-<install git with your package manager>}"
    if [ -n "$command" ] && [ "$INTERACTIVE" = true ] && { : < /dev/tty; } 2>/dev/null; then
      printf "Run it now with sudo? [y/N] "
      local answer=""
      read -r answer < /dev/tty || true
      case "$answer" in
        y|Y|yes|s|S|si|sí) bash -c "$command" < /dev/tty && return 0 ;;
      esac
    fi
    printf "Install git, then run this installer again.\n" >&2
    exit 1
  fi
  if ! command -v git >/dev/null 2>&1; then
    printf "Git is not installed. Attempting to install git...\n"
    if [ -x "$(command -v apt)" ]; then
      apt update && apt install -y git
    elif [ -x "$(command -v yum)" ]; then
      yum install -y git
    elif [ -x "$(command -v dnf)" ]; then
      dnf install -y git
    elif [ -x "$(command -v brew)" ]; then
      brew install git
    else
      printf "Error: Unsupported OS or package manager. Please install git manually.\n" >&2
      exit 1
    fi
  fi
}

# Function to retry git clone in case of network errors
clone_repo() {
  local retries=0
  while [ $retries -lt $MAX_RETRIES ]; do
    printf "Attempting to clone repository (try $((retries + 1))/$MAX_RETRIES)...\n"
    if git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$TARGET_DIR"; then
      return 0  # Success
    else
      printf "Error: Failed to clone the repository on attempt $((retries + 1)).\n"
      retries=$((retries + 1))
    fi
    sleep 5  # Wait before retrying
  done
  return 1  # Failure after retries
}

escape_for_sed() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

sync_config_main_paths() {
  local config_file="$TARGET_DIR/config.yaml"
  local escaped_main
  local escaped_storage
  local escaped_cache
  local escaped_registry
  local escaped_metadata
  local escaped_blockdir
  local escaped_db

  escaped_main="$(escape_for_sed "$TARGET_DIR")"
  escaped_storage="$(escape_for_sed "$TARGET_DIR/storage")"
  escaped_cache="$(escape_for_sed "$TARGET_DIR/storage/__cache__/")"
  escaped_registry="$(escape_for_sed "$TARGET_DIR/storage/__registry__/")"
  escaped_metadata="$(escape_for_sed "$TARGET_DIR/storage/__metadata__/")"
  escaped_blockdir="$(escape_for_sed "$TARGET_DIR/storage/__block__/")"
  escaped_db="$(escape_for_sed "$TARGET_DIR/storage/database.sqlite")"

  sed -i \
    -e "s|^\([[:space:]]*MAIN_DIR:[[:space:]]*\).*|\1\"$escaped_main\"|" \
    -e "s|^\([[:space:]]*STORAGE:[[:space:]]*\).*|\1\"$escaped_storage\"|" \
    -e "s|^\([[:space:]]*CACHE:[[:space:]]*\).*|\1\"$escaped_cache\"|" \
    -e "s|^\([[:space:]]*REGISTRY:[[:space:]]*\).*|\1\"$escaped_registry\"|" \
    -e "s|^\([[:space:]]*METADATA_REGISTRY:[[:space:]]*\).*|\1\"$escaped_metadata\"|" \
    -e "s|^\([[:space:]]*BLOCKDIR:[[:space:]]*\).*|\1\"$escaped_blockdir\"|" \
    -e "s|^\([[:space:]]*DATABASE_FILE:[[:space:]]*\).*|\1\"$escaped_db\"|" \
    "$config_file"
}

if [ "$USE_LOCAL_SOURCE" = true ]; then
  printf "Using local source directory at %s (git clone/pull skipped).\n" "$TARGET_DIR"
  cd "$TARGET_DIR" || { printf "Error: Failed to change directory to $TARGET_DIR.\n" >&2; exit 1; }
else
  # Install git if needed
  install_git_if_needed

  # Increase the Git buffer size to handle large repositories
  git config --global http.postBuffer 524288000  # 500MB
  git config --global http.lowSpeedLimit 0       # Removes the low-speed limit
  git config --global http.lowSpeedTime 999999   # Adjusts the low-speed timeout
  git config --global http.noKeepAlive true      # Disables HTTP keep-alive

  # Check if the target directory already exists
  if [ -d "$TARGET_DIR" ]; then
    if [ "$BRANCH_EXPLICIT" != true ]; then
      CURRENT_BRANCH="$(git -C "$TARGET_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      if [ -n "$CURRENT_BRANCH" ] && [ "$CURRENT_BRANCH" != "HEAD" ]; then
        BRANCH="$CURRENT_BRANCH"
      fi
    fi

    printf "Target directory %s already exists. Updating branch '%s'...\n" "$TARGET_DIR" "$BRANCH"
    cd "$TARGET_DIR" || { printf "Error: Failed to change directory to $TARGET_DIR.\n" >&2; exit 1; }

    if ! git fetch origin "$BRANCH"; then
      printf "Error: Failed to fetch branch '%s' from origin.\n" "$BRANCH" >&2
      exit 1
    fi

    if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
      if ! git checkout "$BRANCH"; then
        printf "Error: Failed to checkout local branch '%s'.\n" "$BRANCH" >&2
        exit 1
      fi
    else
      if ! git checkout -b "$BRANCH" "origin/$BRANCH"; then
        printf "Error: Failed to create local branch '%s' from origin/%s.\n" "$BRANCH" "$BRANCH" >&2
        exit 1
      fi
    fi

    if ! git pull --ff-only origin "$BRANCH"; then
      printf "Error: Failed to pull branch '%s' (non-fast-forward or conflict).\n" "$BRANCH" >&2
      exit 1
    fi
  else
    printf "Cloning branch '%s' from %s into %s...\n" "$BRANCH" "$REPO_URL" "$TARGET_DIR"

    # Try cloning with retries
    if ! clone_repo; then
      printf "Error: Failed to clone the repository after %s attempts.\n" "$MAX_RETRIES" >&2

      # Optionally, try using git:// instead of https://
      printf "Attempting to clone using git:// protocol...\n"
      REPO_URL="git://github.com/celaut-project/nodo.git"
      if ! clone_repo; then
        printf "Error: Failed to clone the repository using git:// protocol as well.\n" >&2
        exit 1
      fi
    fi

    cd "$TARGET_DIR" || { printf "Error: Failed to change directory to $TARGET_DIR.\n" >&2; exit 1; }
  fi
fi

# Create configuration file if it does not exist
if [ ! -f "$TARGET_DIR/config.yaml" ]; then
  printf "Creating configuration file $TARGET_DIR/config.yaml...\n"
  cp "$TARGET_DIR/config.example.yaml" "$TARGET_DIR/config.yaml"
fi
# config.yaml holds the wallet mnemonics and names the binaries the root daemon runs,
# so only its owner (the installing user, after the chown below) and its group may
# read or write it. Also tightens an install made when it was world-writable.
chmod 0660 "$TARGET_DIR/config.yaml"
chmod o-rwx "$TARGET_DIR"/config-*.yaml 2>/dev/null || true

# Keep paths aligned with TARGET_DIR, including local-source installs.
sync_config_main_paths

# Apply custom architecture-specific setup
case "$(uname -m)" in
  aarch64|arm64)  SETUP_SCRIPT="bash/setup_linux_arm.sh" ;;
  x86_64|amd64)   SETUP_SCRIPT="bash/setup_linux_x86.sh" ;;
  *)
    printf "Error: unsupported architecture '%s'. Supported: x86_64/amd64, aarch64/arm64.\n" "$(uname -m)" >&2
    exit 1
    ;;
esac

# resolve_admin_group() lives in bash/lib_pkg.sh so that install.sh and the setup
# scripts cannot disagree about it -- they both render nodo.service.template, and
# when only two of the three renderers knew about {{ADMIN_GROUP}} the third shipped
# a unit systemd could not load. Sourced here rather than at the top of the file:
# the repo only exists under TARGET_DIR after the checkout above.
# shellcheck source=bash/lib_pkg.sh
. "$TARGET_DIR/bash/lib_pkg.sh"
# shellcheck source=bash/lib_privileged.sh
. "$TARGET_DIR/bash/lib_privileged.sh"

if [ "$UNPRIVILEGED" = true ]; then
  SCRIPT_USER="$(id -un)"
else
  SCRIPT_USER="${SUDO_USER:-}"
  if [ -z "$SCRIPT_USER" ]; then
    SCRIPT_USER="$(logname 2>/dev/null || echo root)"
  fi
fi

# Who the daemon runs as. --service-user names it (root: back to a root daemon).
# Without the flag, as on 'nodo update', the node keeps what it has, read from
# root's own record (bash/lib_privileged.sh) and never from config.yaml, which
# that user can write.
if [ "$UNPRIVILEGED" = true ]; then
  # Without sudo there is no other user to run it as.
  SERVICE_USER="$SCRIPT_USER"
elif [ -z "$SERVICE_USER" ]; then
  SERVICE_USER="$(installed_service_user "$SERVICE_FILE")" || exit 1
elif [ "$SERVICE_USER" = root ]; then
  SERVICE_USER=""
fi
# From here on, steps run as root and read config.yaml.
if [ "$UNPRIVILEGED" != true ] && [ -n "$SERVICE_USER" ]; then
  take_config_from_service_user "$TARGET_DIR" "$SERVICE_USER" "$SCRIPT_USER"
fi

PRIVILEGED_SCRIPT="$TARGET_DIR/install-privileged.sh"
if [ "$UNPRIVILEGED" = true ]; then
  NODO_PRIV_FILE="$(mktemp)"
  export NODO_PRIV_FILE NODO_UNPRIVILEGED=1
  trap 'rm -f "$NODO_PRIV_FILE"' EXIT

  # What the setup script needs from the host before it can run: the packages
  # that are missing, and nothing else. A host that has them all is asked nothing.
  detect_pkg_mgr
  read -r -a missing_packages <<< "$(missing_host_packages)"
  command -v newuidmap >/dev/null 2>&1 && command -v newgidmap >/dev/null 2>&1 \
    || case "$PKG_MGR" in dnf) missing_packages+=(shadow-utils) ;; *) missing_packages+=(uidmap) ;; esac
  command -v mksquashfs >/dev/null 2>&1 || missing_packages+=(squashfs-tools)
  if [ "${#missing_packages[@]}" -gt 0 ]; then
    priv_record "Host packages nodo needs: ${missing_packages[*]}" \
      "$(pkg_install_command "${missing_packages[@]}")"
  fi
  if [ "$PKG_MGR" = apt ] && ! host_alias_present locale; then
    priv_record "The en_US.UTF-8 locale" "sudo locale-gen en_US.UTF-8"
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    printf "Error: systemctl was not found. nodo runs as a systemd service; install systemd first.\n" >&2
    exit 1
  fi

  priv_offer "Before it can continue, the installer needs these steps, which need root:" \
    "$INTERACTIVE" "$PRIVILEGED_SCRIPT"
  case $? in
    0) ;;
    *)
      printf "\nRun them, then run this installer again (without sudo).\n" >&2
      exit 1
      ;;
  esac
fi

printf "Running setup script $SETUP_SCRIPT...\n"
if ! /bin/bash "$SETUP_SCRIPT" "$TARGET_DIR" "$CH_VERSION" "$GUEST_KERNEL_VERSION"; then
  printf "Error: The setup script %s failed to execute.\nPlease try running it at least once more. If the issue persists, contact the developers.\n" "$SETUP_SCRIPT" >&2
  exit 1
fi

# No local Docker setup: nodo runs services under Cloud Hypervisor and delegates
# packing to the external packer-service. Docker is never installed on this host.

expand_main_dir_placeholder() {
  printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"
}

read_config_path_or_default() {
  local query="$1"
  local default_value="$2"
  local yq_bin="$TARGET_DIR/bin/yq"
  local config_file="$TARGET_DIR/config.yaml"
  local value=""

  if [ -x "$yq_bin" ] && [ -f "$config_file" ]; then
    value="$("$yq_bin" -r "$query // \"\"" "$config_file" 2>/dev/null || true)"
  fi

  if [ -z "$value" ] || [ "$value" = "null" ]; then
    value="$default_value"
  fi

  expand_main_dir_placeholder "$value"
}

JAVA_HOME_PATH="$(read_config_path_or_default '.dependencies.java.JAVA_HOME' "$TARGET_DIR/runtime/java/current")"
PYTHON_RUNTIME_BIN_PATH="$(read_config_path_or_default '.dependencies.python.RUNTIME_BIN' "$TARGET_DIR/runtime/python/current/bin/python3")"
PYTHON_VENV_BIN_PATH="$(read_config_path_or_default '.dependencies.python.VENV_BIN' "$TARGET_DIR/venv/bin/python")"
PYTHON_RUNTIME_BIN_DIR_PATH="$(dirname "$PYTHON_RUNTIME_BIN_PATH")"

# TEMPORARY: install the portable Java runtime by default, unless it is already
# there. This is provisional, only until the Ergo service is ready (it will then
# bring its own Java, and the node will no longer need one on the host).
install_java_if_needed() {
  if [ -x "$JAVA_HOME_PATH/bin/java" ]; then
    printf "Java already installed at %s. Skipping.\n" "$JAVA_HOME_PATH"
    return 0
  fi

  printf "Java not found at %s. Installing it (provisional, until the Ergo service is ready)...\n" "$JAVA_HOME_PATH"
  if ! /bin/bash "$TARGET_DIR/bash/install_java.sh" "$TARGET_DIR"; then
    printf "Warning: Java installation failed. Retry with: /bin/bash %s/bash/install_java.sh %s\n" "$TARGET_DIR" "$TARGET_DIR" >&2
  fi
}

install_java_if_needed

ADMIN_GROUP="$(resolve_admin_group)"

create_service_file() {
  local expected_file
  local escaped_target
  local escaped_java_home
  local escaped_python_runtime_bin_dir
  local escaped_python_venv_bin
  require_unit_paths "the install directory=$TARGET_DIR" \
    "dependencies.java.JAVA_HOME=$JAVA_HOME_PATH" \
    "dependencies.python.RUNTIME_BIN=$PYTHON_RUNTIME_BIN_DIR_PATH" \
    "dependencies.python.VENV_BIN=$PYTHON_VENV_BIN_PATH"
  require_user_name "the admin group" "$ADMIN_GROUP"
  expected_file="$(mktemp)"
  escaped_target="$(escape_for_sed "$TARGET_DIR")"
  escaped_java_home="$(escape_for_sed "$JAVA_HOME_PATH")"
  escaped_python_runtime_bin_dir="$(escape_for_sed "$PYTHON_RUNTIME_BIN_DIR_PATH")"
  escaped_python_venv_bin="$(escape_for_sed "$PYTHON_VENV_BIN_PATH")"

  # Generate expected service file from template
  sed \
    -e "s|{{MAIN_DIR}}|$escaped_target|g" \
    -e "s|{{JAVA_HOME}}|$escaped_java_home|g" \
    -e "s|{{PYTHON_RUNTIME_BIN_DIR}}|$escaped_python_runtime_bin_dir|g" \
    -e "s|{{PYTHON_VENV_BIN}}|$escaped_python_venv_bin|g" \
    -e "s|{{ADMIN_GROUP}}|$ADMIN_GROUP|g" \
    "$TARGET_DIR/bash/nodo.service.template" > "$expected_file"
  if grep -q '{{[A-Z_][A-Z_]*}}' "$expected_file"; then
    printf "Error: Unresolved placeholders remain in generated service file:\n" >&2
    grep -o '{{[A-Z_][A-Z_]*}}' "$expected_file" | sort -u >&2
    rm -f "$expected_file"
    exit 1
  fi

  if [ -f "$SERVICE_FILE" ] && cmp -s "$SERVICE_FILE" "$expected_file"; then
    printf "Service file %s is already up to date.\n" "$SERVICE_FILE"
    rm -f "$expected_file"
    return
  fi

  if [ -f "$SERVICE_FILE" ]; then
    printf "Service file %s differs from expected. Recreating it...\n" "$SERVICE_FILE"
    systemctl stop nodo.service || true
    systemctl disable nodo.service || true
  fi

  printf "Creating $SERVICE_FILE from template...\n"
  cp "$expected_file" "$SERVICE_FILE"
  rm -f "$expected_file"

  printf "Setting the permissions for the service file...\n"
  chmod 644 "$SERVICE_FILE"

  printf "Reloading systemd daemon, enabling, and starting the nodo service...\n"
  systemctl daemon-reload
  systemctl enable nodo.service
  systemctl start nodo.service
  printf "Systemd daemon reloaded and nodo service started/enabled.\n"
}

# The install without sudo: the daemon runs as the installing user. Everything
# under TARGET_DIR is already theirs, so what is left is what only root can do,
# and each step is asked for only when it is not done already.
prepare_unprivileged_service() {
  local yq_bin="$TARGET_DIR/bin/yq"
  local config_file="$TARGET_DIR/config.yaml"
  local staging="$TARGET_DIR/storage/install"
  local unit="$staging/nodo.service"
  local start

  "$yq_bin" -i '.virtualizers.ch.CGROUPS_BASE_DIR = "/sys/fs/cgroup/system.slice/nodo.service"' "$config_file"
  "$yq_bin" -i '.virtualizers.ch.API_SOCKET_DIR = "/run/nodo/ch"' "$config_file"

  getent group kvm >/dev/null 2>&1 \
    || printf "Warning: group kvm does not exist; KVM is not set up on this host, and the unit cannot start.\n" >&2

  # virtiofsd's namespace sandbox maps the user's subordinate ids.
  if ! has_subids "$SCRIPT_USER"; then
    start="$(next_free_subid_start /etc/subuid)"
    local gstart
    gstart="$(next_free_subid_start /etc/subgid)"
    priv_record "Subordinate ids for $SCRIPT_USER (virtiofsd's sandbox and the local packer)" \
      "sudo usermod --add-subuids $start-$((start + 65535)) --add-subgids $gstart-$((gstart + 65535)) $SCRIPT_USER"
  fi

  mkdir -p "$staging"

  # Where 'sudo nodo update' and 'sudo nodo doctor' learn who the daemon runs as.
  if [ "$(cat "$NODO_SERVICE_USER_RECORD" 2>/dev/null)" != "$SCRIPT_USER" ]; then
    printf '%s' "$SCRIPT_USER" > "$staging/service-user"
    priv_record "Record, in a file only root can write, that nodo runs as $SCRIPT_USER" \
      "sudo install -D -o root -g root -m 0644 $(printf '%q' "$staging/service-user") $NODO_SERVICE_USER_RECORD"
  fi

  # Ubuntu 23.10+: a root-owned copy of virtiofsd, and the profile that lets that
  # copy (and nothing else) create a user namespace. See bash/lib_privileged.sh.
  if apparmor_restricts_userns && [ -x "$TARGET_DIR/bin/virtiofsd" ]; then
    if command -v apparmor_parser >/dev/null 2>&1; then
      if ! cmp -s "$TARGET_DIR/bin/virtiofsd" "$NODO_VIRTIOFSD_SYSTEM_PATH"; then
        priv_record "A root-owned copy of virtiofsd, the one binary the AppArmor profile below lets create a user namespace" \
          "sudo install -D -o root -g root -m 0755 $TARGET_DIR/bin/virtiofsd $NODO_VIRTIOFSD_SYSTEM_PATH"
      fi
      render_virtiofsd_apparmor_profile > "$staging/nodo-virtiofsd.apparmor"
      if ! cmp -s "$staging/nodo-virtiofsd.apparmor" "$NODO_VIRTIOFSD_APPARMOR_PROFILE"; then
        priv_record "The AppArmor profile for that copy (this host restricts unprivileged user namespaces)" \
          "sudo install -m 0644 $staging/nodo-virtiofsd.apparmor $NODO_VIRTIOFSD_APPARMOR_PROFILE && sudo apparmor_parser -r $NODO_VIRTIOFSD_APPARMOR_PROFILE"
      fi
      VIRTIOFSD_PATH="$NODO_VIRTIOFSD_SYSTEM_PATH" "$yq_bin" -i \
        '.virtualizers.ch.VIRTIOFSD_BINARY = strenv(VIRTIOFSD_PATH)' "$config_file"
    else
      printf "Warning: this host restricts unprivileged user namespaces and apparmor_parser was not found: services with shared directories will not start.\n" >&2
    fi
  fi

  require_unit_paths "the install directory=$TARGET_DIR" \
    "dependencies.java.JAVA_HOME=$JAVA_HOME_PATH" \
    "dependencies.python.RUNTIME_BIN=$PYTHON_RUNTIME_BIN_DIR_PATH" \
    "dependencies.python.VENV_BIN=$PYTHON_VENV_BIN_PATH"
  require_user_name "your user name" "$SCRIPT_USER"
  require_user_name "your primary group" "$(id -gn)"
  sed \
    -e "s|{{MAIN_DIR}}|$(escape_for_sed "$TARGET_DIR")|g" \
    -e "s|{{JAVA_HOME}}|$(escape_for_sed "$JAVA_HOME_PATH")|g" \
    -e "s|{{PYTHON_RUNTIME_BIN_DIR}}|$(escape_for_sed "$PYTHON_RUNTIME_BIN_DIR_PATH")|g" \
    -e "s|{{PYTHON_VENV_BIN}}|$(escape_for_sed "$PYTHON_VENV_BIN_PATH")|g" \
    -e "s|{{SERVICE_USER}}|$SCRIPT_USER|g" \
    -e "s|{{SERVICE_GROUP}}|$(id -gn)|g" \
    -e "s|{{RUNTIME_DIR_NAME}}|nodo|g" \
    "$TARGET_DIR/bash/nodo-nosudo.service.template" > "$unit"
  if grep -q '{{[A-Z_][A-Z_]*}}' "$unit"; then
    printf "Error: Unresolved placeholders remain in generated service file:\n" >&2
    grep -o '{{[A-Z_][A-Z_]*}}' "$unit" | sort -u >&2
    exit 1
  fi
  if ! cmp -s "$unit" "$SERVICE_FILE"; then
    priv_record "The systemd unit: runs nodo as $SCRIPT_USER with CAP_NET_ADMIN only, and starts it at boot" \
      "sudo install -m 0644 $unit $SERVICE_FILE && sudo systemctl daemon-reload && sudo systemctl enable nodo.service"
  fi
  # Last, so the node starts on everything above, and on the code just installed.
  priv_record "Start nodo (or restart it on the new code)" "sudo systemctl restart nodo.service"
}

# A --service-user install gets its unit from bash/setup_service_user.sh instead,
# after the chown below: that chown would otherwise take the storage tree back.
if [ "$UNPRIVILEGED" = true ]; then
  prepare_unprivileged_service
elif [ -z "$SERVICE_USER" ]; then
  write_service_user_record ""
  create_service_file
fi

create_wrapper_script() {
  WRAPPER_SCRIPT="/usr/local/bin/nodo"
  if [ "$UNPRIVILEGED" = true ]; then
    WRAPPER_SCRIPT="$HOME/.local/bin/nodo"
    mkdir -p "$(dirname "$WRAPPER_SCRIPT")"
  fi

  # Check if the wrapper script already exists and remove it
  if [ -f "$WRAPPER_SCRIPT" ]; then
    printf "Wrapper script %s already exists. Removing it...\n" "$WRAPPER_SCRIPT"
    rm -f "$WRAPPER_SCRIPT"
  fi

  printf "Creating %s...\n" "$WRAPPER_SCRIPT"

  # Create the wrapper script. Note:
  # - $TARGET_DIR is not escaped so it is expanded at compile time.
  # - \$PWD and \$ORIGINAL_DIR are escaped to be evaluated at runtime.
  cat <<EOF > "$WRAPPER_SCRIPT"
#!/bin/bash
# ORIGINAL_DIR is set to the current directory at runtime
ORIGINAL_DIR="\$PWD"
# Change directory to TARGET_DIR, which was expanded at compile time
cd "$TARGET_DIR" || exit
# Use local Java/Python runtimes installed under TARGET_DIR
export JAVA_HOME="$JAVA_HOME_PATH"
export PATH="$JAVA_HOME_PATH/bin:$PYTHON_RUNTIME_BIN_DIR_PATH:$TARGET_DIR/bin:\$PATH"
# Activate the virtual environment in TARGET_DIR
source "$TARGET_DIR/venv/bin/activate"
# Execute the Python script with the runtime ORIGINAL_DIR and passed arguments
ORIGINAL_DIR="\$ORIGINAL_DIR" "$PYTHON_VENV_BIN_PATH" "$TARGET_DIR/nodo.py" "\$@"
EOF

  # Make the wrapper script executable
  chmod +x "$WRAPPER_SCRIPT"

  printf "Wrapper script %s created.\n" "$WRAPPER_SCRIPT"
  case ":$PATH:" in
    *":$(dirname "$WRAPPER_SCRIPT"):"*) ;;
    *) printf "Note: %s is not in your PATH; add it to use the 'nodo' command.\n" "$(dirname "$WRAPPER_SCRIPT")" ;;
  esac
}

create_wrapper_script

install_shell_completion() {
  # Install bash/zsh tab-completion for commands and service/instance/peer ids.
  # Invoke the helper directly (not via the nodo wrapper) so this never triggers
  # nodo's heavy import graph or the KYA prompt during install.
  # Without root, completion.py installs into the user's own completion dirs.
  local scope="--system"
  [ "$UNPRIVILEGED" != true ] || scope=""
  printf "Installing shell completion...\n"
  # shellcheck disable=SC2086
  NODO_COMPLETION_DIR="$TARGET_DIR" NODO_COMPLETION_PY="$PYTHON_VENV_BIN_PATH" \
    "$PYTHON_VENV_BIN_PATH" "$TARGET_DIR/src/commands/completion.py" install $scope \
    || printf "Shell completion install skipped (non-fatal).\n"
}

install_shell_completion

set_donation_percentage_if_named() {
  # The share is NOT asked for here. It is asked on the node's first run
  # (src/commands/onboarding.py), beside the KyA, because an install is not reliably a
  # place where a question can be asked: on Windows this script is piped into bash by
  # install.ps1, and under Nodo-Setup.exe there is no console at all. A prompt there is
  # a prompt into a pipe -- skipped in silence, default kept, which is the one outcome
  # "a default nobody is told about is not consent" was meant to rule out.
  #
  # What survives here is the scripted path: naming the share in the environment still
  # sets it, so an unattended fleet install does not have to answer anything twice.
  # Nothing is written when it is not named, and config.yaml keeps its default.
  local yq_bin="$TARGET_DIR/bin/yq"
  local config_file="$TARGET_DIR/config.yaml"
  local key='.ledgers.ergo.payments.DONATION_PERCENTAGE'
  local answer="${NODO_DONATION_PERCENTAGE:-}"

  [ -n "$answer" ] || return 0
  [ -x "$yq_bin" ] && [ -f "$config_file" ] || return 0

  answer="$(printf '%s' "$answer" | tr -d '[:space:]')"
  # A share, so a fraction of one. A malformed answer is refused rather than coerced:
  # "2" meant as a percentage would donate everything.
  if ! printf '%s' "$answer" | grep -Eq '^(0|1)(\.[0-9]+)?$|^\.[0-9]+$'; then
    printf "NODO_DONATION_PERCENTAGE='%s' is not a share between 0 and 1; ignored.\n" "$answer"
    return 0
  fi

  if "$yq_bin" -i "$key = \"$answer\"" "$config_file" 2>/dev/null; then
    printf "Donation share set to %s from the environment.\n" "$answer"
    # Answered, so the first run must not ask again.
    mkdir -p "$TARGET_DIR/storage" 2>/dev/null || true
    touch "$TARGET_DIR/storage/.askeddonation" 2>/dev/null || true
  else
    printf "Could not write the donation share; config.yaml keeps its current value.\n"
  fi
}

set_donation_percentage_if_named

assign_gateway_port() {
  # Explicitly, here: this is the last moment in an install that has root AND an
  # operator watching the terminal, and opening the port is something only they can
  # finish. Before the chown, so the config.yaml this writes ends up owned like
  # every other file. Invoked directly, not through the nodo wrapper, so it does not
  # pull in the heavy import graph or the KYA prompt. Non-fatal by construction.
  printf "Assigning the gateway port...\n"
  "$PYTHON_VENV_BIN_PATH" "$TARGET_DIR/src/commands/assign_gateway_port.py" "$TARGET_DIR" \
    || printf "Gateway port not assigned; 'sudo nodo serve' will report why.\n"
}

if [ "$UNPRIVILEGED" = true ]; then
  # Opening the port needs CAP_NET_ADMIN, which the daemon has and this shell
  # does not: the node assigns it when the unit starts it.
  GATEWAY_NOTICE="$TARGET_DIR/.gateway_notice"
  rm -f "$GATEWAY_NOTICE"
  printf "\nnodo is installed in %s.\n" "$TARGET_DIR"
  priv_offer "These last steps need root:" "$INTERACTIVE" "$PRIVILEGED_SCRIPT"
  case $? in
    0)
      printf "\nnodo runs as %s with CAP_NET_ADMIN only. 'nodo doctor' checks the node; 'systemctl status nodo' shows the service.\n" "$SCRIPT_USER"
      ;;
    2)
      printf "\nThe steps that did not run are in %s. Run it to finish: bash %s\n" "$PRIVILEGED_SCRIPT" "$PRIVILEGED_SCRIPT" >&2
      exit 1
      ;;
    *)
      if [ -s "$PRIVILEGED_SCRIPT" ]; then
        printf "\nRun them to finish: bash %s\n" "$PRIVILEGED_SCRIPT"
      fi
      ;;
  esac
  exit 0
fi

assign_gateway_port

chown -R "$SCRIPT_USER:$SCRIPT_USER" "$TARGET_DIR"

if [ -n "$SERVICE_USER" ]; then
  /bin/bash "$TARGET_DIR/bash/setup_service_user.sh" \
    --target-dir "$TARGET_DIR" --user "$SERVICE_USER" --operator "$SCRIPT_USER" \
    --java-home "$JAVA_HOME_PATH" \
    --python-runtime-bin-dir "$PYTHON_RUNTIME_BIN_DIR_PATH" \
    --python-venv-bin "$PYTHON_VENV_BIN_PATH" || exit 1
  systemctl enable nodo.service
fi

if systemctl list-unit-files --type=service | grep -Fq "nodo.service"; then
  printf "Restarting nodo.service...\n"
  systemctl restart nodo.service || systemctl start nodo.service
else
  printf "Error: nodo.service does not exist or cannot be restarted. Please check the service creation process.\n" >&2
fi

printf "Installation and service setup completed successfully. The repository is located at $TARGET_DIR.\n"
printf "********** You can now use the 'nodo' command. **********\n"

# Last, deliberately. The gateway port is the one thing the node cannot work
# without, and its alert is written while a helper above loads the config -- so
# everything printed since (completion, chown, systemctl, the two lines above)
# stood between the operator and the only message here that asks them to do
# something. In a terminal the last line is the one that gets read.
GATEWAY_NOTICE="$TARGET_DIR/.gateway_notice"
if [ -s "$GATEWAY_NOTICE" ]; then
  cat "$GATEWAY_NOTICE"
fi
