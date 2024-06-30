#!/bin/bash

# Check if the script is running with root privileges
if [ "$(id -u)" -ne 0 ]; then
  echo "Error: This script needs to be run with sudo."
  echo "Please run the following command to run this script with sudo:"
  echo "  sudo $0"
  exit 1
fi

# Function to install git if it's not already installed
install_git_if_needed() {
  if ! command -v git >/dev/null 2>&1; then
    echo "Git is not installed. Attempting to install git..."

    # Detect the operating system and install git
    if [ -x "$(command -v apt)" ]; then
      # Debian/Ubuntu-based
      apt update && apt install -y git
    elif [ -x "$(command -v yum)" ]; then
      # Red Hat/CentOS-based
      yum install -y git
    elif [ -x "$(command -v dnf)" ]; then
      # Fedora-based
      dnf install -y git
    elif [ -x "$(command -v brew)" ]; then
      # macOS
      brew install git
    else
      echo "Error: Unsupported OS or package manager. Please install git manually."
      exit 1
    fi
  fi
}

# Install git if needed
install_git_if_needed

# Define the repository URL and the target directory
REPO_URL="https://github.com/celaut-project/nodo.git"
TARGET_DIR="/nodo"

# Check if the target directory already exists
if [ -d "$TARGET_DIR" ]; then
  echo "Target directory $TARGET_DIR already exists. Performing git pull..."
  cd "$TARGET_DIR" || { echo "Error: Failed to change directory to $TARGET_DIR."; exit 1; }
  if ! git pull; then
    echo "Error: Failed to perform git pull."
    exit 1
  fi
else
  # Clone the repository into the target directory
  echo "Cloning repository from $REPO_URL into $TARGET_DIR..."
  if ! git clone $REPO_URL $TARGET_DIR; then
    echo "Error: Failed to clone the repository."
    exit 1
  fi
  # Navigate to the cloned repository directory
  cd $TARGET_DIR || { echo "Error: Failed to change directory to $TARGET_DIR."; exit 1; }
fi

# Check the platform architecture and set the setup script accordingly
if [ "$(uname -m)" = "armv7l" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_arm.sh"
else
  SETUP_SCRIPT="bash/setup_ubuntu_x86.sh"
fi

# Make sure the setup script is executable
chmod +x $SETUP_SCRIPT

# Execute the setup script
echo "Running setup script $SETUP_SCRIPT..."
if ! ./$SETUP_SCRIPT "$TARGET_DIR"; then
  echo "Error: The setup script $SETUP_SCRIPT failed to execute."
  exit 1
fi

# Get the user who executed the script
SCRIPT_USER=$(logname)

# Function to create nodo.service if it doesn't exist
create_service_file() {
  SERVICE_FILE="/etc/systemd/system/nodo.service"
  
  # Remove existing service file if it already exists
  if [ -f "$SERVICE_FILE" ]; then
    echo "Service file $SERVICE_FILE already exists. Removing it..."
    systemctl stop nodo.service
    systemctl disable nodo.service
    rm -f "$SERVICE_FILE"
  fi

  # Create the service file
  echo "Creating $SERVICE_FILE..."
  cat <<EOF > $SERVICE_FILE
[Unit]
Description=Nodo Serve
After=network.target

[Service]
Type=simple
User=root
Group=sudo
WorkingDirectory=$TARGET_DIR
ExecStart=/bin/bash -c 'source $TARGET_DIR/venv/bin/activate && exec python3 $TARGET_DIR/nodo.py service'
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  # Set the permissions for the service file
  chmod 644 $SERVICE_FILE

  # Reload systemd, enable and start the service
  echo "Reloading systemd, enabling, and starting the nodo service..."
  systemctl daemon-reload
  systemctl enable nodo.service
  systemctl start nodo.service
}

# Create nodo.service
create_service_file

# Function to create a wrapper script for nodo
create_wrapper_script() {
  WRAPPER_SCRIPT="/usr/local/bin/nodo"

  # Remove existing wrapper script if it already exists
  if [ -f "$WRAPPER_SCRIPT" ]; then
    echo "Wrapper script $WRAPPER_SCRIPT already exists. Removing it..."
    rm -f "$WRAPPER_SCRIPT"
  fi

  # Create the wrapper script
  echo "Creating $WRAPPER_SCRIPT..."
  cat <<EOF > $WRAPPER_SCRIPT
#!/bin/bash
cd $TARGET_DIR || exit
source $TARGET_DIR/venv/bin/activate
python3 $TARGET_DIR/nodo.py "\$@"
EOF

  # Set the permissions for the wrapper script
  chmod +x $WRAPPER_SCRIPT
}

# Create wrapper script
create_wrapper_script

chown -R $SCRIPT_USER:$SCRIPT_USER $TARGET_DIR

echo "Installation and service setup completed successfully. The repository is located at $TARGET_DIR."
echo "********** You can now use the 'nodo' command. **********"

