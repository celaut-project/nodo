#!/bin/bash

if [ -z "${BASH_VERSION:-}" ]; then
  printf "Error: This installer requires bash. Run: bash install.sh\n" >&2
  exit 1
fi

# Check if the script is running with root privileges
if [ "$(id -u)" -ne 0 ]; then
  printf "Error: This script needs to be run with sudo.\nPlease run: sudo $0\n" >&2
  exit 1
fi

# Defaults
REPO_URL="https://github.com/celaut-project/nodo.git"
TARGET_DIR="/nodo"
SERVICE_FILE="/etc/systemd/system/nodo.service"
MAX_RETRIES=3
USE_LOCAL_SOURCE=false
BRANCH="stable"
BRANCH_EXPLICIT=false
CH_VERSION="v51.1"
# Guest kernel + busybox published by .github/workflows/guest-kernel.yml; bumped independently
# of nodo releases so a kernel fix does not require cutting a node release.
GUEST_KERNEL_VERSION="guest-kernel"

print_usage() {
  cat <<EOF
Usage: sudo ./install.sh [options]

Options:
  --source-dir <path>  Use an existing local source directory instead of cloning.
  --target-dir <path>  Install/clone into this directory (default: /nodo).
  --repo-url <url>     Repository URL for clone/pull.
  --branch <name>      Git branch for clone/pull (default: stable).
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
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
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
  chmod a+w "$TARGET_DIR/config.yaml"
fi

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

printf "Running setup script $SETUP_SCRIPT...\n"
if ! /bin/bash "$SETUP_SCRIPT" "$TARGET_DIR" "$CH_VERSION" "$GUEST_KERNEL_VERSION"; then
  printf "Error: The setup script %s failed to execute.\nPlease try running it at least once more. If the issue persists, contact the developers.\n" "$SETUP_SCRIPT" >&2
  exit 1
fi

# No local Docker setup: nodo runs services under Cloud Hypervisor and delegates
# packing to the external packer-service. Docker is never installed on this host.

SCRIPT_USER="${SUDO_USER:-}"
if [ -z "$SCRIPT_USER" ]; then
  SCRIPT_USER="$(logname 2>/dev/null || echo root)"
fi

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

# resolve_admin_group() lives in bash/lib_pkg.sh so that install.sh and the setup
# scripts cannot disagree about it -- they both render nodo.service.template, and
# when only two of the three renderers knew about {{ADMIN_GROUP}} the third shipped
# a unit systemd could not load. Sourced here rather than at the top of the file:
# the repo only exists under TARGET_DIR after the checkout above.
# shellcheck source=bash/lib_pkg.sh
. "$TARGET_DIR/bash/lib_pkg.sh"

ADMIN_GROUP="$(resolve_admin_group)"

create_service_file() {
  local expected_file
  local escaped_target
  local escaped_java_home
  local escaped_python_runtime_bin_dir
  local escaped_python_venv_bin
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

create_service_file

create_wrapper_script() {
  WRAPPER_SCRIPT="/usr/local/bin/nodo"

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
}

create_wrapper_script

install_shell_completion() {
  # Install bash/zsh tab-completion for commands and service/instance/peer ids.
  # Invoke the helper directly (not via the nodo wrapper) so this never triggers
  # nodo's heavy import graph or the KYA prompt during install.
  printf "Installing shell completion...\n"
  NODO_COMPLETION_DIR="$TARGET_DIR" NODO_COMPLETION_PY="$PYTHON_VENV_BIN_PATH" \
    "$PYTHON_VENV_BIN_PATH" "$TARGET_DIR/src/commands/completion.py" install --system \
    || printf "Shell completion install skipped (non-fatal).\n"
}

install_shell_completion

chown -R "$SCRIPT_USER:$SCRIPT_USER" "$TARGET_DIR"

if systemctl list-unit-files --type=service | grep -Fq "nodo.service"; then
  printf "Restarting nodo.service...\n"
  systemctl restart nodo.service || systemctl start nodo.service
else
  printf "Error: nodo.service does not exist or cannot be restarted. Please check the service creation process.\n" >&2
fi

printf "Installation and service setup completed successfully. The repository is located at $TARGET_DIR.\n"
printf "********** You can now use the 'nodo' command. **********\n"
