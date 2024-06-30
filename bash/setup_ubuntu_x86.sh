#!/bin/bash

set -e  # Salir inmediatamente si un comando falla.

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
sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
                        libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev > /dev/null

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

python3 -m pip install https://github.com/reputation-systems/reputation-graph-service/raw/master/target/wheels/reputation_graph-0.0.0-cp311-cp311-manylinux_2_35_x86_64.whl || {
    echo "System not compatible with the reputation library; only basic reputation functionality is supported."
}

echo "Installing required system packages for Docker..."
sudo apt-get -y install ca-certificates curl gnupg lsb-release > /dev/null

echo "Adding Docker GPG key and repository..."
if [ ! -f /usr/share/keyrings/docker-archive-keyring.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg > /dev/null
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

echo "Updating package lists again..."

# Try to execute the update command, redirecting both stdout and stderr to /dev/null
# Capture the exit code of the command in the variable $?
sudo apt-get -y update > /dev/null 2>&1
exit_code=$?

# Check the exit code
if [ $exit_code -ne 0 ]; then
    echo "Error: Failed to update package lists. Exit code: $exit_code"
    # You can add additional actions here if needed upon error
else
    echo "Package lists updated successfully."
fi

# Check if Docker is already installed
echo "Check if Docker is already installed"
if command -v docker > /dev/null 2>&1; then
    echo "Docker is already installed."
    docker --version
else
    echo "Installing Docker..."
    sudo apt-get update > /dev/null
    sudo apt-get -y install docker-ce docker-ce-cli containerd.io > /dev/null

    echo "Installing QEMU and binfmt-support for multi-architecture support..."
    sudo apt-get -y install qemu-system binfmt-support qemu-user-static > /dev/null

    # Configure QEMU for multi-architecture support
    docker run --rm --privileged multiarch/qemu-user-static --reset -p yes > /dev/null
fi

# Check if rustc is already installed
echo "Check if rustc is already installed"
if command -v rustc > /dev/null 2>&1; then
    echo "Rust is already installed."
    rustc --version
else
    echo "Installing Rust (Cargo)..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y > /dev/null

    echo "Sourcing the Rust environment..."
    source $HOME/.cargo/env
fi

echo "Executing initialization script for x86..."
sh ./bash/init_x86.sh > /dev/null

echo "Running migrations for Python application..."
python3.11 nodo.py migrate > /dev/null

echo "All steps completed."
