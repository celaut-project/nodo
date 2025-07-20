#!/bin/bash

# Check for root
if [ "$(id -u)" -ne 0 ]; then
  printf "Error: This script needs to be run with sudo.\nPlease run: sudo $0\n" >&2
  exit 1
fi

OS_TYPE="$(uname)"
SCRIPT_USER="${SUDO_USER:-$USER}"

# Set target directory in user's home
TARGET_DIR="/home/$SCRIPT_USER/nodo"
if [ "$OS_TYPE" = "Darwin" ]; then
  TARGET_DIR="/Users/$SCRIPT_USER/nodo"
fi

REPO_URL="https://github.com/celaut-project/nodo.git"
MAX_RETRIES=3
SERVICE_FILE_LINUX="/etc/systemd/system/nodo.service"

# Install git if needed
install_git_if_needed() {
  if ! command -v git >/dev/null 2>&1; then
    printf "Git is not installed. Installing...\n"
    if [ "$OS_TYPE" = "Linux" ]; then
      if command -v apt >/dev/null 2>&1; then
        apt update && apt install -y git
      elif command -v yum >/dev/null 2>&1; then
        yum install -y git
      elif command -v dnf >/dev/null 2>&1; then
        dnf install -y git
      else
        printf "Unsupported Linux package manager. Install git manually.\n" >&2
        exit 1
      fi
    elif [ "$OS_TYPE" = "Darwin" ]; then
      if command -v brew >/dev/null 2>&1; then
        brew install git
      else
        printf "Homebrew not found. Please install Git manually.\n" >&2
        exit 1
      fi
    else
      printf "Unsupported OS: $OS_TYPE\n" >&2
      exit 1
    fi
  fi
}

install_git_if_needed

# Git config tweaks
git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999
git config --global http.noKeepAlive true

clone_repo() {
  local retries=0
  while [ $retries -lt $MAX_RETRIES ]; do
    printf "Cloning repo (try $((retries + 1)))...\n"
    if git clone "$REPO_URL" "$TARGET_DIR"; then
      return 0
    fi
    retries=$((retries + 1))
    sleep 5
  done
  return 1
}

# Clone or pull
if [ -d "$TARGET_DIR" ]; then
  printf "Directory exists. Pulling updates...\n"
  cd "$TARGET_DIR" || exit 1
  if ! git pull; then
    printf "Failed to pull repo.\n" >&2
    exit 1
  fi
else
  if ! clone_repo; then
    printf "Clone via HTTPS failed. Trying git://...\n"
    REPO_URL="git://github.com/celaut-project/nodo.git"
    if ! clone_repo; then
      printf "Clone failed. Exiting.\n" >&2
      exit 1
    fi
  fi
  cd "$TARGET_DIR" || exit 1
fi

# Create config file if missing
if [ ! -f "config.yaml" ]; then
  printf "Creating config.yaml...\n"
  cp config.example.yaml config.yaml
  chmod a+w config.yaml
fi

# Architecture-specific setup
ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_arm.sh"
elif [ "$ARCH" = "x86_64" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_x86.sh"
else
  printf "Unsupported architecture: $ARCH\n" >&2
  exit 1
fi

chmod +x "$SETUP_SCRIPT"
printf "Running setup script: $SETUP_SCRIPT...\n"
if ! ./"$SETUP_SCRIPT" "$TARGET_DIR"; then
  printf "Setup script failed.\n" >&2
  exit 1
fi

# Linux-only systemd service
if [ "$OS_TYPE" = "Linux" ]; then
  if [ -f "$SERVICE_FILE_LINUX" ]; then
    printf "Removing old service...\n"
    systemctl stop nodo.service
    systemctl disable nodo.service
    rm -f "$SERVICE_FILE_LINUX"
  fi

  cat <<EOF > "$SERVICE_FILE_LINUX"
[Unit]
Description=Nodo Serve
After=network.target

[Service]
Type=simple
User=$SCRIPT_USER
WorkingDirectory=$TARGET_DIR
ExecStart=/bin/bash -c 'source $TARGET_DIR/venv/bin/activate && exec python3 $TARGET_DIR/nodo.py daemon'
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  chmod 644 "$SERVICE_FILE_LINUX"
  systemctl daemon-reload
  systemctl enable nodo.service
  systemctl start nodo.service
else
  printf "⚠️  Skipping service creation: systemd is not available on macOS.\n"
fi

# Wrapper script for all OS
WRAPPER_SCRIPT="/usr/local/bin/nodo"
if [ -f "$WRAPPER_SCRIPT" ]; then
  rm -f "$WRAPPER_SCRIPT"
fi

cat <<EOF > "$WRAPPER_SCRIPT"
#!/bin/bash
ORIGINAL_DIR="\$PWD"
cd "$TARGET_DIR" || exit
source "$TARGET_DIR/venv/bin/activate"
ORIGINAL_DIR="\$ORIGINAL_DIR" python3 "$TARGET_DIR/nodo.py" "\$@"
EOF

chmod +x "$WRAPPER_SCRIPT"

# Restore script
RESTORE_SCRIPT="bash/restore_source.sh"
chmod +x "$RESTORE_SCRIPT"
if ! ./"$RESTORE_SCRIPT" "$TARGET_DIR"; then
  printf "Restore script failed.\n" >&2
  exit 1
fi

chown -R "$SCRIPT_USER:$SCRIPT_USER" "$TARGET_DIR"
chmod -R 755 "$TARGET_DIR"

if [ "$OS_TYPE" = "Linux" ]; then
  systemctl restart nodo.service
fi

# Final steps
ACCEPT_KYA_SCRIPT="bash/accept_kya.sh"
chmod +x "$ACCEPT_KYA_SCRIPT"
./"$ACCEPT_KYA_SCRIPT"

printf "\n✅ Installation complete. Repo in $TARGET_DIR\n"
printf "👉 You can now run: nodo\n"
