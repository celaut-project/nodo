#!/bin/bash

# Update package lists
apt-get -y update

# Install Python 3 and pip
apt-get -y install python3 python3-venv python3-pip

# Create and activate a Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies from requirements.txt
pip3 install -r requirements.txt

echo "Installing required system packages for Podman..."
sudo apt-get -y install ca-certificates curl gnupg lsb-release > /dev/null

echo "Updating package lists..."
sudo apt-get -y update > /dev/null 2>&1 || {
    echo "Error updating package lists."
    exit 1
}

# Check if Podman (or related packages) is installed and remove if necessary
if command -v podman > /dev/null 2>&1; then
    PODMAN_VERSION=$(podman --version | grep -oP '\d+\.\d+\.\d+')
    echo "Podman version $PODMAN_VERSION is installed. Removing Podman..."
    sudo apt-get -y remove podman podman-plugins > /dev/null
else
    echo "Podman is not installed."
fi

# Installing Podman if it's not installed or was removed
if ! command -v podman > /dev/null 2>&1; then
    echo "Installing Podman..."
    sudo apt-get -y install podman > /dev/null
else
    echo "Podman is already installed."
fi

echo "Podman installation completed successfully."

# Install Rust (Cargo)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# Source the Rust environment
source $HOME/.cargo/env

# Execute initialization script (assuming it's in the current directory)
sh ./bash/init_arm.sh

# Run migrations for Python application
python3 nodo.py migrate
