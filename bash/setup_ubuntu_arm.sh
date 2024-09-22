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

# Install required system packages for Docker
apt-get -y install \
    ca-certificates \
    curl \
    gnupg \
    lsb-release

# Add Docker GPG key and repository
if [ ! -f /usr/share/keyrings/docker-archive-keyring.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Update package lists again
apt-get -y update

# Install Docker
apt-get -y install docker-ce docker-ce-cli containerd.io

# Install Rust (Cargo)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# Source the Rust environment
source $HOME/.cargo/env

# Execute initialization script (assuming it's in the current directory)
sh ./bash/init_arm.sh

# Run migrations for Python application
python3 nodo.py migrate