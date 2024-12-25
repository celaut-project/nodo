#!/bin/bash

# Check if the script is running with root privileges
if [ "$(id -u)" -ne 0 ]; then
  printf "Error: This script needs to be run with sudo.\nPlease run: sudo $0\n" >&2
  exit 1
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

# Install git if needed
install_git_if_needed

# Define the repository URL and the target directory
REPO_URL="https://github.com/celaut-project/nodo.git"
TARGET_DIR="/nodo"
SERVICE_FILE="/etc/systemd/system/nodo.service"
MAX_RETRIES=3

# Increase the Git buffer size to handle large repositories
git config --global http.postBuffer 524288000  # 500MB
git config --global http.lowSpeedLimit 0       # Removes the low-speed limit
git config --global http.lowSpeedTime 999999   # Adjusts the low-speed timeout
git config --global http.noKeepAlive true      # Disables HTTP keep-alive

# Function to retry git clone in case of network errors
clone_repo() {
  local retries=0
  while [ $retries -lt $MAX_RETRIES ]; do
    printf "Attempting to clone repository (try $((retries + 1))/$MAX_RETRIES)...\n"
    if git clone "$REPO_URL" "$TARGET_DIR"; then
      return 0  # Success
    else
      printf "Error: Failed to clone the repository on attempt $((retries + 1)).\n"
      retries=$((retries + 1))
    fi
    sleep 5  # Wait before retrying
  done
  return 1  # Failure after retries
}

# Check if the target directory already exists
if [ -d "$TARGET_DIR" ]; then
  printf "Target directory $TARGET_DIR already exists. Performing git pull...\n"
  cd "$TARGET_DIR" || { printf "Error: Failed to change directory to $TARGET_DIR.\n" >&2; exit 1; }
  if ! git pull; then
    printf "Error: Failed to perform git pull.\n" >&2
    exit 1
  fi

  if systemctl list-units --full -all | grep -Fq "nodo.service"; then
    printf "Restarting nodo.service...\n"
    systemctl restart nodo.service
  else
    printf "nodo.service does not exist, will create it later in the script.\n"
  fi
else
  printf "Cloning repository from $REPO_URL into $TARGET_DIR...\n"

  # Try cloning with retries
  if ! clone_repo; then
    printf "Error: Failed to clone the repository after $MAX_RETRIES attempts.\n" >&2

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

if [ "$(uname -m)" = "armv7l" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_arm.sh"
else
  SETUP_SCRIPT="bash/setup_ubuntu_x86.sh"
fi

chmod +x "$SETUP_SCRIPT"

printf "Running setup script $SETUP_SCRIPT...\n"
if ! ./"$SETUP_SCRIPT" "$TARGET_DIR"; then
  printf "Error: The setup script $SETUP_SCRIPT failed to execute.\n" >&2
  exit 1
fi

SCRIPT_USER=$(logname)

create_service_file() {
  if [ -f "$SERVICE_FILE" ]; then
    printf "Service file $SERVICE_FILE already exists. Removing it...\n"
    systemctl stop nodo.service
    systemctl disable nodo.service
    rm -f "$SERVICE_FILE"
  fi

  printf "Creating $SERVICE_FILE...\n"
  cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Nodo Serve
After=network.target

[Service]
Type=simple
User=root
Group=sudo
WorkingDirectory=$TARGET_DIR
ExecStart=/bin/bash -c 'source $TARGET_DIR/venv/bin/activate && exec python3 $TARGET_DIR/nodo.py daemon'
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  printf "Setting the permissions for the service file...\n"
  chmod 644 "$SERVICE_FILE"

  printf "Reloading systemd daemon, enabling, and starting the nodo service...\n"
  systemctl daemon-reload
  systemctl enable nodo.service
  systemctl start nodo.service
  printf "Systemd daemon reloaded and nodo service started/enabled.\n"
}

if [ ! -f "$SERVICE_FILE" ]; then
  printf "nodo.service does not exist. Creating service file...\n"
  create_service_file
else
  printf "nodo.service already exists. Checking its status...\n"
  systemctl status nodo.service || printf "Service is not running or not correctly installed.\n"
fi

create_wrapper_script() {
  WRAPPER_SCRIPT="/usr/local/bin/nodo"

  if [ -f "$WRAPPER_SCRIPT" ]; then
    printf "Wrapper script $WRAPPER_SCRIPT already exists. Removing it...\n"
    rm -f "$WRAPPER_SCRIPT"
  fi

  printf "Creating $WRAPPER_SCRIPT...\n"
  cat <<EOF > "$WRAPPER_SCRIPT"
#!/bin/bash
cd $TARGET_DIR || exit
source $TARGET_DIR/venv/bin/activate
python3 $TARGET_DIR/nodo.py "\$@"
EOF

  chmod +x "$WRAPPER_SCRIPT"

  printf "Setting permissions for $TARGET_DIR and wrapper script...\n"
  chmod -R 777 "$TARGET_DIR"
  chmod +x "$WRAPPER_SCRIPT"
}

create_wrapper_script

chown -R "$SCRIPT_USER:$SCRIPT_USER" "$TARGET_DIR"
chmod -R 777 "$TARGET_DIR"

RESTORE_SCRIPT="bash/restore_src.sh"
chmod +x "$RESTORE_SCRIPT"
if ! ./"$RESTORE_SCRIPT" "$TARGET_DIR"; then
  printf "Error: The script $RESTORE_SCRIPT failed to execute.\n" >&2
  exit 1
fi

if systemctl status nodo.service >/dev/null 2>&1; then
  printf "Restarting nodo.service...\n"
  systemctl restart nodo.service
else
  printf "Error: nodo.service does not exist or cannot be restarted. Please check the service creation process.\n" >&2
fi

# Accept KyA
ACCEPT_KYA_SCRIPT="bash/accept_kya.sh"
chmod +x "$ACCEPT_KYA_SCRIPT"
if ! ./"$ACCEPT_KYA_SCRIPT" "$TARGET_DIR"; then
  printf "Error: The script $ACCEPT_KYA_SCRIPT failed to execute.\n" >&2
  exit 1
fi

printf "Installation and service setup completed successfully. The repository is located at $TARGET_DIR.\n"
printf "********** You can now use the 'nodo' command. **********\n"
