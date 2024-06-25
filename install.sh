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
  echo "Error: Target directory $TARGET_DIR already exists. Please remove it or choose another location."
  exit 1
fi

# Clone the repository into the target directory
echo "Cloning repository from $REPO_URL into $TARGET_DIR..."
if ! git clone $REPO_URL $TARGET_DIR; then
  echo "Error: Failed to clone the repository."
  exit 1
fi

# Navigate to the cloned repository directory
cd $TARGET_DIR || { echo "Error: Failed to change directory to $TARGET_DIR."; exit 1; }

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
if ! ./$SETUP_SCRIPT; then
  echo "Error: The setup script $SETUP_SCRIPT failed to execute."
  exit 1
fi

# Function to create nodo.service if it doesn't exist
create_service_file() {
  SERVICE_FILE="/etc/systemd/system/nodo.service"
  
  # Check if a similar service already exists
  if systemctl list-units --full --all | grep -q "nodo.service"; then
    echo "Error: A service with the name nodo.service already exists."
    exit 1
  fi

  # Get the user who executed the script
  SCRIPT_USER=$(logname)

  # Create the service file
  echo "Creating $SERVICE_FILE..."
  cat <<EOF > $SERVICE_FILE
[Unit]
Description=Nodo Serve
After=network.target

[Service]
Type=simple
User=$SCRIPT_USER
Group=sudo
WorkingDirectory=$TARGET_DIR
ExecStart=/bin/bash -c 'source $TARGET_DIR/venv/bin/activate && exec python3 $TARGET_DIR/nodo.py serve'
Restart=on-failure

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

echo "Installation and service setup completed successfully. The repository is located at $TARGET_DIR."
