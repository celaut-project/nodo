#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ] && [ "${NODO_ROOTLESS:-0}" -ne 1 ]; then
    echo "Please run this script as root or with sudo."
    exit 1
fi

TARGET_DIR="${1:-}"
if [ -z "$TARGET_DIR" ]; then
    echo "Error: You must pass the project root directory as the first argument."
    exit 1
fi

handle_apt_error() {
    local code=$1
    echo "apt-get error (code $code)."
    case "$code" in
        100)
            echo "  - Lock file issue. Removing locks and retrying..."
            rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock*
            dpkg --configure -a
            ;;
        200)
            echo "  - GPG authentication error. Check your keys."
            ;;
        *)
            echo "  - Unknown APT error."
            ;;
    esac
}

echo "1. Updating package lists..."
echo "Note: sudo is required to update the system package index so we can install dependencies."
sudo apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
    || { handle_apt_error $?; sudo apt-get update; }

echo "2. Installing build dependencies and basic tools..."
echo "Note: sudo is required to install system-level build tools and libraries."
sudo apt-get install -y --no-install-recommends \
    build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
    libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev \
    ca-certificates curl gnupg lsb-release software-properties-common \
    git procps locales \
    > /dev/null || { handle_apt_error $?; exit 1; }

echo "3. Ensuring UTF-8 locale support..."
echo "Note: sudo is required to generate locales."
sudo locale-gen en_US.UTF-8 >/dev/null || true
sudo update-locale LANG=en_US.UTF-8

echo "4. Installing yq for YAML parsing..."
if ! command -v yq >/dev/null; then
    echo "Note: sudo is required to install 'yq' into /usr/local/bin."
    sudo wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_arm64
    sudo chmod +x /usr/local/bin/yq
    echo "   - yq installed."
else
    echo "   - yq already installed."
fi

echo "5. Adding Deadsnakes PPA for Python 3.11..."
echo "Note: sudo is required to add the deadsnakes PPA."
sudo add-apt-repository ppa:deadsnakes/ppa -y >/dev/null

echo "6. Updating package lists after adding PPA..."
sudo apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
    || { handle_apt_error $?; sudo apt-get update; }

echo "7. Installing Python 3.11 and venv modules..."
echo "Note: sudo is required to install Python 3.11 system packages."
sudo apt-get install -y python3.11 python3.11-venv python3.11-distutils >/dev/null

echo "8. Installing pip for Python 3.11..."
wget -qO get-pip.py https://bootstrap.pypa.io/get-pip.py
echo "Note: sudo is required to install pip globally for Python 3.11."
sudo python3.11 get-pip.py >/dev/null
rm get-pip.py

echo "9. Creating and activating virtualenv at $TARGET_DIR/venv..."
cd "$TARGET_DIR"
python3.11 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate

REQ_FILE="$TARGET_DIR/bash/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    echo "Error: requirements.txt not found at $REQ_FILE"
    deactivate
    exit 1
fi

echo "10. Installing Python dependencies..."
pip install --upgrade pip >/dev/null
if ! pip install -r "$REQ_FILE" >/dev/null; then
    echo "   - Failed to install Python packages."
    deactivate
    exit 1
fi

echo "11. Installing OpenJDK 21..."
echo "Note: sudo is required to install the OpenJDK 21 JRE."
sudo apt-get install -y openjdk-21-jre-headless >/dev/null

if [ "${NODO_ROOTLESS:-0}" -ne 1 ]; then
    echo "12. Setting up Docker (v24) for ARM..."
    # Add Docker GPG key if missing
    if [ ! -f /usr/share/keyrings/docker-archive-keyring.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
    fi
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
       https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list

    apt-get update >/dev/null

    # Remove outdated Docker if present
    if command -v docker >/dev/null; then
        ver=$(docker --version | grep -oP '\d+\.\d+\.\d+')
        if [[ $ver != 24.* ]]; then
            echo "   - Removing Docker $ver..."
            sudo apt-get remove -y docker docker-engine docker.io containerd runc >/dev/null
        fi
    fi

    # Install Docker 24.*
    echo "Note: sudo is required to install Docker system packages."
    sudo apt-get install -y --allow-downgrades docker-ce=5:24.* docker-ce-cli=5:24.* containerd.io >/dev/null
else
    echo "12. Skipping system Docker installation (NODO_ROOTLESS is set)."
fi

echo "13. (Optional) Installing QEMU/binfmt for multi-architecture containers..."
echo "Note: This step requires sudo to install system packages."
sudo apt-get install -y qemu-user-static binfmt-support >/dev/null


if [ "${NODO_ROOTLESS:-0}" -ne 1 ]; then
    echo "   - Configuring QEMU with Docker..."
    docker run --rm --privileged multiarch/qemu-user-static --reset -p yes >/dev/null
else
    echo "   - Skipping QEMU Docker configuration (NODO_ROOTLESS is set)."
fi


echo "14. Installing Rust (cargo)…"
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
else
    echo "   - Rust already installed."
fi

echo "15. Running ARM-specific init script..."
if [ -x "$TARGET_DIR/bash/init_arm.sh" ]; then
    sh "$TARGET_DIR/bash/init_arm.sh"
else
    echo "   - init_arm.sh not found or not executable."
fi

echo "16. Running database migrations with Python..."
python3.11 nodo.py migrate >/dev/null || {
    echo "   - Migration failed."
    deactivate
    exit 1
}

echo "ARM setup completed successfully!"
deactivate
