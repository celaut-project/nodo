#!/bin/bash

# Define the repository URL and the setup script
REPO_URL="https://github.com/celaut-project/nodo.git"
SETUP_SCRIPT="bash/setup_ubuntu_x86.sh"
TARGET_DIR="$HOME/nodo"

# Function to install git
install_git() {
  echo "Git is not installed. Attempting to install git..."

  # Detect the operating system and install git
  if [ -x "$(command -v apt)" ]; then
    # Debian/Ubuntu-based
    if [ "$(id -u)" -ne 0 ]; then
      echo "Error: This script needs to be run with sudo to install git using apt."
      echo "Please run the following commands to install git manually:"
      echo "  sudo apt update"
      echo "  sudo apt install -y git"
      exit 1
    else
      sudo apt update && sudo apt install -y git
    fi
  elif [ -x "$(command -v yum)" ]; then
    # Red Hat/CentOS-based
    if [ "$(id -u)" -ne 0 ]; then
      echo "Error: This script needs to be run with sudo to install git using yum."
      echo "Please run the following commands to install git manually:"
      echo "  sudo yum install -y git"
      exit 1
    else
      sudo yum install -y git
    fi
  elif [ -x "$(command -v dnf)" ]; then
    # Fedora-based
    if [ "$(id -u)" -ne 0 ]; then
      echo "Error: This script needs to be run with sudo to install git using dnf."
      echo "Please run the following commands to install git manually:"
      echo "  sudo dnf install -y git"
      exit 1
    else
      sudo dnf install -y git
    fi
  elif [ -x "$(command -v brew)" ]; then
    # macOS
    brew install git
  else
    echo "Error: Unsupported OS or package manager. Please install git manually."
    exit 1
  fi
}

# Check if git is installed
if ! command -v git &> /dev/null; then
  install_git
  if ! command -v git &> /dev/null; then
    echo "Error: Failed to install git. Please install it manually."
    exit 1
  fi
fi

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

# Make sure the setup script is executable
chmod +x $SETUP_SCRIPT

# Execute the setup script
echo "Running setup script $SETUP_SCRIPT..."
if ! ./$SETUP_SCRIPT; then
  echo "Error: The setup script $SETUP_SCRIPT failed to execute."
  exit 1
fi

echo "Installation completed successfully. The repository is located at $TARGET_DIR."
