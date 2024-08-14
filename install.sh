#!/bin/bash

# Function to install git if it's not already installed
install_git_if_needed() {
  if ! command -v git >/dev/null 2>&1; then
    printf "Git is not installed. Attempting to install git...\n"

    # Detect the operating system and install git
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
      return 1
    fi
  fi
}

# Install git if needed
install_git_if_needed || exit 1

# Define the repository URL and the target directory
REPO_URL="https://github.com/celaut-project/nodo.git"
TARGET_DIR="$HOME/nodo"
SERVICE_FILE="$HOME/.config/systemd/user/nodo.service"

# Check if the target directory already exists
if [ -d "$TARGET_DIR" ]; then
  printf "Target directory %s already exists. Performing git pull...\n" "$TARGET_DIR"
  cd "$TARGET_DIR" || { printf "Error: Failed to change directory to %s.\n" "$TARGET_DIR" >&2; exit 1; }
  if ! git pull; then
    printf "Error: Failed to perform git pull.\n" >&2
    exit 1
  fi
else
  # Clone the repository into the target directory
  printf "Cloning repository from %s into %s...\n" "$REPO_URL" "$TARGET_DIR"
  if ! git clone "$REPO_URL" "$TARGET_DIR"; then
    printf "Error: Failed to clone the repository.\n" >&2
    exit 1
  fi
  cd "$TARGET_DIR" || { printf "Error: Failed to change directory to %s.\n" "$TARGET_DIR" >&2; exit 1; }
fi

# Check the platform architecture and set the setup script accordingly
if [ "$(uname -m)" = "armv7l" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_arm.sh"
else
  SETUP_SCRIPT="bash/setup_ubuntu_x86.sh"
fi

# Make sure the setup script is executable
chmod +x "$SETUP_SCRIPT"

# Execute the setup script
printf "Running setup script %s...\n" "$SETUP_SCRIPT"
if ! ./"$SETUP_SCRIPT" "$TARGET_DIR"; then
  printf "Error: The setup script %s failed to execute.\n" "$SETUP_SCRIPT" >&2
  exit 1
fi

# Function to create nodo.service if it doesn't exist
create_service_file() {
  # Remove existing service file if it already exists
  if [ -f "$SERVICE_FILE" ]; then
    printf "Service file %s already exists. Removing it...\n" "$SERVICE_FILE"
    systemctl --user stop nodo.service
    systemctl --user disable nodo.service
    rm -f "$SERVICE_FILE"
  fi

  # Create the service file
  printf "Creating %s...\n" "$SERVICE_FILE"
  mkdir -p "$(dirname "$SERVICE_FILE")"
  cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Nodo Serve
After=network.target

[Service]
Type=simple
WorkingDirectory=$TARGET_DIR
ExecStart=/bin/bash -c 'source $TARGET_DIR/venv/bin/activate && exec python3 $TARGET_DIR/nodo.py service'
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

  # Set the permissions for the service file
  chmod 644 "$SERVICE_FILE"

  # Reload systemd, enable and start the service
  printf "Reloading systemd daemon, enabling, and starting the nodo service...\n"
  systemctl --user daemon-reload
  systemctl --user enable nodo.service
  systemctl --user start nodo.service
  printf "Systemd daemon reloaded and nodo service started/enabled.\n"
}

# Check if the service file exists
if [ ! -f "$SERVICE_FILE" ]; then
  printf "nodo.service does not exist. Creating service file...\n"
  create_service_file
else
  printf "nodo.service already exists. Checking its status...\n"
  systemctl --user status nodo.service || printf "Service is not running or not correctly installed.\n"
fi

# Restart the service if it exists
if systemctl --user status nodo.service >/dev/null 2>&1; then
  printf "Restarting nodo.service...\n"
  systemctl --user restart nodo.service
else
  printf "Error: nodo.service does not exist or cannot be restarted. Please check the service creation process.\n" >&2
fi

# Function to create a wrapper script for nodo
create_wrapper_script() {
  WRAPPER_SCRIPT="$HOME/.local/bin/nodo"
  mkdir -p "$(dirname "$WRAPPER_SCRIPT")"

  # Remove existing wrapper script if it already exists
  if [ -f "$WRAPPER_SCRIPT" ]; then
    printf "Wrapper script %s already exists. Removing it...\n" "$WRAPPER_SCRIPT"
    rm -f "$WRAPPER_SCRIPT"
  fi

  # Create the wrapper script
  printf "Creating %s...\n" "$WRAPPER_SCRIPT"
  cat <<EOF > "$WRAPPER_SCRIPT"
#!/bin/bash
cd $TARGET_DIR || exit
source $TARGET_DIR/venv/bin/activate
python3 $TARGET_DIR/nodo.py "\$@"
EOF

  # Set the permissions for the wrapper script
  chmod +x "$WRAPPER_SCRIPT"
}

# Create wrapper script
create_wrapper_script

UPDATE_ENV_SCRIPT="bash/update_env.sh"
chmod +x "$UPDATE_ENV_SCRIPT"
printf "Updating envs %s...\n" "$UPDATE_ENV_SCRIPT"
if ! ./"$UPDATE_ENV_SCRIPT" "$TARGET_DIR"; then
  printf "Error: The script %s failed to execute.\n" "$UPDATE_ENV_SCRIPT" >&2
  exit 1
fi

chown -R "$USER":"$USER" "$TARGET_DIR"

printf "Installation and service setup completed successfully. The repository is located at %s.\n" "$TARGET_DIR"
printf "********** You can now use the 'nodo' command. **********\n"
