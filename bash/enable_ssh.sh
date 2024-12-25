#!/bin/sh

# Function to display messages (using POSIX compatible syntax)
message() {
    printf '\033[1;34m%s\033[0m\n' "$1"
}

# Function to display errors
error() {
    printf '\033[1;31m%s\033[0m\n' "$1" >&2
}

# Check if the script is run as root
if [ "$(id -u)" != "0" ]; then
    error "This script must be run as root. Please use sudo or log in as root."
    exit 1
fi

# Check the OS version
if ! command -v lsb_release >/dev/null 2>&1; then
    error "lsb_release command not found. Please install lsb-release package."
    exit 1
fi

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
if systemctl is-active ssh >/dev/null 2>&1; then
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

# Get the IP address of the server
IP_ADDRESS=$(hostname -I | awk '{print $1}')

# Get the actual username (even when running with sudo)
if [ -n "$SUDO_USER" ]; then
    ACTUAL_USER=$SUDO_USER
else
    ACTUAL_USER=$(whoami)
fi

message "Installation complete. Here's how to connect to your SSH server:"
message "1. From another Linux/Mac terminal use:"
message "   ssh $ACTUAL_USER@$IP_ADDRESS"
message ""
message "2. From Windows:"
message "   - Use PuTTY: Enter $IP_ADDRESS in the Host Name field and $ACTUAL_USER as username"
message "   - Or use PowerShell/Windows Terminal: ssh $ACTUAL_USER@$IP_ADDRESS"
message ""
message "Your connection details:"
message "- IP address: $IP_ADDRESS"
message "- Username: $ACTUAL_USER"
message "- Port: 22 (default)"

exit 0