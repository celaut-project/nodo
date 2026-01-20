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

UPDATED_APT=false

ensure_apt_updated() {
    if [ "$UPDATED_APT" = false ]; then
        echo "Updating package lists..."
        echo "Note: sudo is required to update the system package index so we can install dependencies."
        sudo apt-get -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false update > /dev/null 2>&1 || {
            handle_update_errors $?
        }
        UPDATED_APT=true
    fi
}

check_packages() {
    dpkg -s "$@" > /dev/null 2>&1
}

echo "Checking build dependencies..."
BUILD_DEPS="build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev"

if check_packages $BUILD_DEPS; then
    echo "Build dependencies already installed."
else
    echo "Installing required build dependencies..."
    ensure_apt_updated
    echo "Note: sudo is required to install system-level build tools and libraries (build-essential, etc.)."
    if sudo apt-get install -y $BUILD_DEPS > /dev/null 2>&1; then
        echo "Dependencies installed successfully."
    else
        echo "Error installing dependencies. Attempting to fix broken dependencies..."
        if sudo apt --fix-broken install -y > /dev/null 2>&1; then
            echo "Fixed broken dependencies. Retrying to install required build dependencies..."
            if sudo apt-get install -y $BUILD_DEPS > /dev/null 2>&1; then
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
fi

echo "Installing yq for YAML processing..."
if ! command -v yq &> /dev/null; then
    echo "Note: sudo is required to install 'yq' into /usr/local/bin."
    sudo wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq
    sudo chmod +x /usr/local/bin/yq
fi

echo "Checking Python 3.11..."
if check_packages python3.11 python3.11-venv python3.11-distutils; then
    echo "Python 3.11 and modules already installed."
else
    echo "Adding Python 3.11 repository..."
    echo "Note: sudo is required to add the deadsnakes PPA for Python 3.11."
    sudo add-apt-repository ppa:deadsnakes/ppa -y > /dev/null
    
    # Force update after adding PPA
    UPDATED_APT=false
    ensure_apt_updated

    echo "Installing Python 3.11 and pip..."
    echo "Note: sudo is required to install Python 3.11 system packages."
    sudo apt-get -y install python3.11 python3.11-venv python3.11-distutils > /dev/null
fi

echo "Installing pip for Python 3.11..."
# Check if pip is installed for python3.11? 
# It's hard to check pip specifically without running python, but let's assume if we have the venv we might be good, 
# but the script installs it manually. Let's check if we can run python3.11 -m pip
if python3.11 -m pip --version > /dev/null 2>&1; then
    echo "pip for Python 3.11 already installed."
else
    wget -q https://bootstrap.pypa.io/get-pip.py -O get-pip.py
    echo "Note: sudo is required to install pip globally for Python 3.11."
    sudo python3.11 get-pip.py > /dev/null
    rm get-pip.py
fi

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

echo "Checking OpenJDK 21..."
if check_packages openjdk-21-jre-headless; then
    echo "OpenJDK 21 already installed."
else
    echo "Installing OpenJDK 21"
    ensure_apt_updated
    echo "Note: sudo is required to install the OpenJDK 21 JRE."
    sudo apt-get -y install openjdk-21-jre-headless
fi

if [ "$NODO_ROOTLESS" != "1" ]; then
    echo "Installing required system packages for Docker ..."
    sudo apt-get -y install ca-certificates curl gnupg lsb-release > /dev/null

    echo "Updating package lists..."
    sudo apt-get -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false update > /dev/null 2>&1 || {
        handle_update_errors $?
    }

    echo "Installing required system packages for Docker..."
    sudo apt-get -y install ca-certificates curl gnupg lsb-release > /dev/null

    echo "Adding Docker GPG key and repository..."
    if [ ! -f /usr/share/keyrings/docker-archive-keyring.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg > /dev/null
    fi
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    echo "Updating package lists again..."
    sudo apt-get -y update > /dev/null 2>&1 || {
        handle_update_errors $?
    }

    # Check if Docker is already installed and its version
    if command -v docker > /dev/null 2>&1; then
        DOCKER_VERSION=$(docker --version | grep -oP '\d+\.\d+\.\d+')
        if [[ "$DOCKER_VERSION" != 24* ]]; then
            echo "Docker version $DOCKER_VERSION is installed. Removing it..."
            sudo apt-get -y remove docker docker-engine docker.io containerd runc > /dev/null
        else
            echo "Docker version 24 is already installed."
        fi
    fi

    if ! command -v docker > /dev/null 2>&1 || [[ "$DOCKER_VERSION" != 24* ]]; then
        echo "Installing Docker version 24..."
        sudo apt-get -y --allow-downgrades install docker-ce=5:24.* docker-ce-cli=5:24.* containerd.io > /dev/null
    fi
else
    echo "Skipping system Docker installation (NODO_ROOTLESS is set)."
fi


echo "Checking QEMU and binfmt-support..."
if check_packages qemu-system binfmt-support qemu-user-static; then
    echo "QEMU and binfmt-support already installed."
else
    echo "Installing QEMU and binfmt-support for multi-architecture support..."
    ensure_apt_updated
    echo "Note: This step requires sudo to install system packages (qemu-system, binfmt-support, qemu-user-static)."
    sudo apt-get -y install qemu-system binfmt-support qemu-user-static > /dev/null
fi

# Configure QEMU for multi-architecture support
if [ "$NODO_ROOTLESS" != "1" ]; then
    echo "Configuring QEMU with Docker..."
    docker run --rm --privileged multiarch/qemu-user-static --reset -p yes > /dev/null
else
    echo "Skipping QEMU Docker configuration (NODO_ROOTLESS is set)."
    echo "Multi-architecture support might be limited without privileged QEMU configuration."
fi


echo "Executing initialization script for x86..."
sh ./bash/init_x86.sh > /dev/null

echo "Running migrations for Python application..."
chmod +x bash/accept_kya.sh
python3.11 nodo.py migrate > /dev/null

echo "All steps completed."
