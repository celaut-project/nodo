#!/bin/sh

# Ask for the target directory with a default value
echo "Enter the target directory [default: /nodo]: "
read TARGET_DIR
TARGET_DIR=${TARGET_DIR:-/nodo}

# Check if the target directory already exists and ask for confirmation before deleting
if [ -d "$TARGET_DIR" ]; then
  echo "Target directory $TARGET_DIR already exists. Do you want to delete it? [y/N]: "
  read confirm
  case "$confirm" in
    [Yy]*)
      echo "Removing existing directory $TARGET_DIR..."
      sudo rm -rf "$TARGET_DIR"
      ;;
    *)
      echo "Installation aborted. Please specify a different target directory."
      exit 1
      ;;
  esac
fi

# Function to install git if it's not already installed
install_git_if_needed() {
  if ! command -v git >/dev/null 2>&1; then
    echo "Git is not installed. Attempting to install git..."

    # Detect the operating system and install git
    if [ -x "$(command -v apt)" ]; then
      # Debian/Ubuntu-based
      sudo apt update && sudo apt install -y git
    elif [ -x "$(command -v yum)" ]; then
      # Red Hat/CentOS-based
      sudo yum install -y git
    elif [ -x "$(command -v dnf)" ]; then
      # Fedora-based
      sudo dnf install -y git
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

# Define the repository URL
REPO_URL="https://github.com/celaut-project/nodo.git"

# Clone the repository into the target directory
echo "Cloning repository from $REPO_URL into $TARGET_DIR..."
if ! git clone $REPO_URL "$TARGET_DIR"; then
  echo "Error: Failed to clone the repository."
  exit 1
fi

# Navigate to the cloned repository directory
cd "$TARGET_DIR" || { echo "Error: Failed to change directory to $TARGET_DIR."; exit 1; }

# Check the platform architecture and set the setup script accordingly
if [ "$(uname -m)" = "armv7l" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_arm.sh"
else
  SETUP_SCRIPT="bash/setup_ubuntu_x86.sh"
fi

# Make sure the setup script is executable
chmod +x "$SETUP_SCRIPT"

# Execute the setup script
echo "Running setup script $SETUP_SCRIPT..."
if ! ./"$SETUP_SCRIPT"; then
  echo "Error: The setup script $SETUP_SCRIPT failed to execute."
  exit 1
fi

# Function to create nodo.service if it doesn't exist
create_service_file() {
  SERVICE_FILE="/etc/systemd/system/nodo.service"
  
  # Check if the service file already exists and ask for confirmation before deleting
  if [ -f "$SERVICE_FILE" ]; then
    echo "Service file $SERVICE_FILE already exists. Do you want to delete it? [y/N]: "
    read confirm
    case "$confirm" in
      [Yy]*)
        echo "Stopping and removing existing service $SERVICE_FILE..."
        sudo systemctl stop nodo.service
        sudo systemctl disable nodo.service
        sudo rm -f "$SERVICE_FILE"
        ;;
      *)
        echo "Service setup aborted. Please manually handle the existing service file."
        exit 1
        ;;
    esac
  fi

  # Get the user who executed the script
  SCRIPT_USER=$(logname)

  # Create the service file
  echo "Creating $SERVICE_FILE..."
  sudo sh -c "cat <<EOF > $SERVICE_FILE
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
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF"

  # Set the permissions for the service file
  sudo chmod 644 "$SERVICE_FILE"

  # Reload systemd, enable and start the service
  echo "Reloading systemd, enabling, and starting the nodo service..."
  sudo systemctl daemon-reload
  sudo systemctl enable nodo.service
  sudo systemctl start nodo.service
}

# Create nodo.service
create_service_file

echo "Installation and service setup completed successfully. The repository is located at $TARGET_DIR."
