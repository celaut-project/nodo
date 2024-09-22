#!/bin/bash

set -e

if [ -z "$1" ]; then
  echo "Error: TARGET_DIR is not provided."
  exit 1
fi

TARGET_DIR="$1"

handle_update_errors() {
    exit_code=$1
    echo "Failed to update package lists. Exit code: $exit_code"

    case $exit_code in
        100)
            echo "Lock file exists, maybe another package manager is running. Attempting to remove lock file and retrying..."
            sudo rm /var/lib/apt/lists/lock
            ;;
        200)
            echo "Authentication error. Verify if GPG keys are properly added."
            ;;
        *)
            echo "Unknown error occurred during package update."
            ;;
    esac
}

echo "Updating package lists..."
sudo apt-get -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false update > /dev/null 2>&1 || {
    handle_update_errors $?
}

echo "Installing required build dependencies..."
if sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
                           libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev > /dev/null 2>&1; then
    echo "Dependencies installed successfully."
else
    echo "Error installing dependencies. Attempting to fix broken dependencies..."
    if sudo apt --fix-broken install -y > /dev/null 2>&1; then
        echo "Fixed broken dependencies. Retrying to install required build dependencies..."
        if sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
                                   libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev > /dev/null 2>&1; then
            echo "Dependencies installed successfully after fixing broken dependencies."
        else
            echo "Failed to install dependencies after fixing broken dependencies. Please check manually."
            exit 1
        fi
    else
        echo "Failed to fix broken dependencies. Please check manually."
        exit 1
    fi
fi

echo "Adding Python 3.11 repository..."
sudo add-apt-repository ppa:deadsnakes/ppa -y > /dev/null

echo "Updating package lists after adding Python repository..."
sudo apt-get -y update > /dev/null 2>&1 || {
    handle_update_errors $?
}

echo "Installing Python 3.11 and pip..."
sudo apt-get -y install python3.11 python3.11-venv python3.11-distutils > /dev/null

echo "Installing pip for Python 3.11..."
wget -q https://bootstrap.pypa.io/get-pip.py -O get-pip.py
sudo python3.11 get-pip.py > /dev/null
rm get-pip.py

echo "Creating and activating Python virtual environment..."
python3.11 -m venv venv
source venv/bin/activate

REQUIREMENTS_FILE="$TARGET_DIR/bash/requirements.txt"

# Check if requirements.txt exists
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "Error: requirements.txt not found at $REQUIREMENTS_FILE"
    deactivate
    exit 1
fi

echo "Installing Python dependencies from $REQUIREMENTS_FILE..."
if ! python3 -m pip install -r "$REQUIREMENTS_FILE" > /dev/null; then
    echo "Error: Failed to install Python packages from requirements.txt."
    deactivate
    exit 1
fi

echo "Installing OpenJDK 21"
sudo apt-get -y install openjdk-21-jre-headless

echo "Installing required system packages for Podman..."
sudo apt-get -y install ca-certificates curl gnupg lsb-release > /dev/null

echo "Updating package lists..."
sudo apt-get -y update > /dev/null 2>&1 || {
    echo "Error updating package lists."
    exit 1
}

# Check if Podman is installed
if command -v podman > /dev/null 2>&1; then
    PODMAN_VERSION=$(podman --version | grep -oP '\d+\.\d+\.\d+')
    echo "Podman version $PODMAN_VERSION is installed. Removing Podman..."
    sudo apt-get -y remove podman > /dev/null
else
    echo "Podman is not installed."
fi

# Check if podman-plugins is installed
if dpkg -s podman-plugins > /dev/null 2>&1; then
    echo "Podman-plugins is installed. Removing podman-plugins..."
    sudo apt-get -y remove podman-plugins > /dev/null
else
    echo "Podman-plugins is not installed."
fi

# Installing Podman if it's not installed or was removed
if ! command -v podman > /dev/null 2>&1; then
    echo "Installing Podman..."
    sudo apt-get -y install podman > /dev/null
else
    echo "Podman is already installed."
fi

# Configure default registry for unqualified images
REGISTRY_CONF="/etc/containers/registries.conf"

if grep -q '\[registries.search\]' "$REGISTRY_CONF"; then
    echo "Modifying existing registry configuration..."
    sudo sed -i '/\[registries.search\]/!b;n;c\registries = [\"docker.io\"]' "$REGISTRY_CONF"
else
    echo "Adding registry configuration..."
    sudo bash -c "echo -e '\n[registries.search]\nregistries = [\"docker.io\"]' >> $REGISTRY_CONF"
fi

echo "Podman installation completed successfully."

echo "Installing QEMU and binfmt-support for multi-architecture support..."
sudo apt-get -y install qemu-system binfmt-support qemu-user-static > /dev/null

# Configure QEMU for multi-architecture support
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes > /dev/null

echo "Executing initialization script for x86..."
sh ./bash/init_x86.sh > /dev/null

echo "Running migrations for Python application..."
python3.11 nodo.py migrate > /dev/null

echo "All steps completed."
