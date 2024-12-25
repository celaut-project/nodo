#!/bin/bash

# Function to display messages
function message() {
    echo -e "\e[1;34m$1\e[0m"
}

# Function to display errors
function error() {
    echo -e "\e[1;31m$1\e[0m" >&2
}

# Check if the script is run as root
if [ "$(id -u)" -ne 0 ]; then
    error "This script must be run as root. Please use sudo or log in as root."
    exit 1
fi

# Check the OS version
OS=$(lsb_release -is)
VERSION=$(lsb_release -rs)

if [ "$OS" != "Ubuntu" ] || [ "$VERSION" != "22.04" ]; then
    error "This script is only compatible with Ubuntu 22.04. Detected: $OS $VERSION"
    exit 1
fi

message "Detected Ubuntu 22.04. Proceeding with SSH server installation..."

# Update package lists
message "Updating package lists..."
apt update -y

# Upgrade installed packages
message "Upgrading installed packages..."
apt upgrade -y

# Install OpenSSH server
message "Installing OpenSSH server..."
apt install -y openssh-server

# Enable and start SSH service
message "Enabling and starting SSH service..."
systemctl enable ssh
systemctl start ssh

# Verify installation
if systemctl is-active --quiet ssh; then
    message "OpenSSH server installed and running successfully!"
else
    error "Failed to start the SSH server. Please check system logs for details."
    exit 1
fi

# Allow SSH through UFW if installed
if command -v ufw >/dev/null 2>&1; then
    message "Allowing SSH through the firewall..."
    ufw allow ssh
fi

message "Installation complete. You can now connect via SSH."
exit 0
