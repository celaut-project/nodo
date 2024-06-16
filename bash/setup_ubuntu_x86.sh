#!/bin/bash

echo "Updating package lists..."
apt-get -y update > /dev/null

echo "Installing Python 3.11 and pip..."
apt-get -y install python3.11 python3.11-venv python3-pip > /dev/null

echo "Creating and activating Python virtual environment..."
python3.11 -m venv venv
source venv/bin/activate

echo "Installing Python dependencies from requirements.txt..."
pip3 install -r requirements.txt > /dev/null

echo "Installing required system packages for Docker..."
apt-get -y install \
    ca-certificates \
    curl \
    gnupg \
    lsb-release > /dev/null

echo "Adding Docker GPG key and repository..."
if [ ! -f /usr/share/keyrings/docker-archive-keyring.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg > /dev/null
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

echo "Updating package lists again..."
apt-get -y update > /dev/null

echo "Installing Docker..."
apt-get -y install docker-ce docker-ce-cli containerd.io > /dev/null

echo "Installing QEMU and binfmt-support for multi-architecture support..."
apt-get -y install qemu binfmt-support qemu-user-static > /dev/null
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes > /dev/null

echo "Installing Rust (Cargo)..."
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y > /dev/null

echo "Sourcing the Rust environment..."
source $HOME/.cargo/env

echo "Executing initialization script for x86..."
sh ./bash/init_x86.sh > /dev/null

echo "Running migrations for Python application..."
python3 nodo.py migrate > /dev/null

echo "All steps completed."
