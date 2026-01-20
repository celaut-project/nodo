#!/bin/bash

# Function to install git if it's not already installed
install_git_if_needed() {
  if ! command -v git >/dev/null 2>&1; then
    printf "Git is not installed. Attempting to install git...\n"
    if [ -x "$(command -v apt)" ]; then
      sudo apt update && sudo apt install -y git
    elif [ -x "$(command -v yum)" ]; then
      sudo yum install -y git
    elif [ -x "$(command -v dnf)" ]; then
      sudo dnf install -y git
    elif [ -x "$(command -v brew)" ]; then
      brew install git
    else
      printf "Error: Unsupported OS or package manager. Please install git manually.\n" >&2
      exit 1
    fi
  fi
}

# Install git if needed
install_git_if_needed

# Define the repository URL and the target directory
REPO_URL="https://github.com/celaut-project/nodo.git"

# Check if the script is running with root privileges
if [ "$(id -u)" -ne 0 ]; then
  printf "Running in rootless mode (installing to $HOME/.nodo)\n"
  TARGET_DIR="$HOME/.nodo"
  USE_SUDO=false
else
  TARGET_DIR="/nodo"
  USE_SUDO=true
fi

SERVICE_FILE="/etc/systemd/system/nodo.service"
MAX_RETRIES=3

# Increase the Git buffer size to handle large repositories
git config --global http.postBuffer 524288000  # 500MB
git config --global http.lowSpeedLimit 0       # Removes the low-speed limit
git config --global http.lowSpeedTime 999999   # Adjusts the low-speed timeout
git config --global http.noKeepAlive true      # Disables HTTP keep-alive

# Function to retry git clone in case of network errors
clone_repo() {
  local retries=0
  while [ $retries -lt $MAX_RETRIES ]; do
    printf "Attempting to clone repository (try $((retries + 1))/$MAX_RETRIES)...\n"
    if git clone "$REPO_URL" "$TARGET_DIR"; then
      return 0  # Success
    else
      printf "Error: Failed to clone the repository on attempt $((retries + 1)).\n"
      retries=$((retries + 1))
    fi
    sleep 5  # Wait before retrying
  done
  return 1  # Failure after retries
}

# Check if the target directory already exists
if [ -d "$TARGET_DIR" ]; then
  printf "Target directory $TARGET_DIR already exists. Performing git pull...\n"
  cd "$TARGET_DIR" || { printf "Error: Failed to change directory to $TARGET_DIR.\n" >&2; exit 1; }
  if ! git pull; then
    printf "Error: Failed to perform git pull.\n" >&2
    exit 1
  fi

  if [ "$USE_SUDO" = true ] && systemctl list-units --full -all | grep -Fq "nodo.service"; then
    printf "Restarting nodo.service...\n"
    sudo systemctl restart nodo.service
  fi
else
  printf "Cloning repository from $REPO_URL into $TARGET_DIR...\n"

  # Try cloning with retries
  if ! clone_repo; then
    printf "Error: Failed to clone the repository after $MAX_RETRIES attempts.\n" >&2

    # Optionally, try using git:// instead of https://
    printf "Attempting to clone using git:// protocol...\n"
    REPO_URL="git://github.com/celaut-project/nodo.git"
    if ! clone_repo; then
      printf "Error: Failed to clone the repository using git:// protocol as well.\n" >&2
      exit 1
    fi
  fi

  cd "$TARGET_DIR" || { printf "Error: Failed to change directory to $TARGET_DIR.\n" >&2; exit 1; }
fi

# Create configuration file if it does not exist
if [ ! -f "$TARGET_DIR/config.yaml" ]; then
  printf "Creating configuration file $TARGET_DIR/config.yaml...\n"
  cp "$TARGET_DIR/config.example.yaml" "$TARGET_DIR/config.yaml"
  # Update MAIN_DIR in config.yaml
  if command -v yq >/dev/null 2>&1; then
    yq -i ".main.MAIN_DIR = \"$TARGET_DIR\"" "$TARGET_DIR/config.yaml"
    yq -i ".main.STORAGE = \"$TARGET_DIR/storage\"" "$TARGET_DIR/config.yaml"
    yq -i ".main.CACHE = \"$TARGET_DIR/storage/__cache__/\"" "$TARGET_DIR/config.yaml"
    yq -i ".main.REGISTRY = \"$TARGET_DIR/storage/__registry__/\"" "$TARGET_DIR/config.yaml"
    yq -i ".main.METADATA_REGISTRY = \"$TARGET_DIR/storage/__metadata__/\"" "$TARGET_DIR/config.yaml"
    yq -i ".main.BLOCKDIR = \"$TARGET_DIR/storage/__block__/\"" "$TARGET_DIR/config.yaml"
    yq -i ".main.DATABASE_FILE = \"$TARGET_DIR/storage/database.sqlite\"" "$TARGET_DIR/config.yaml"
    yq -i ".ledgers.ergo.HTTP_PEERS_PATH = \"$TARGET_DIR/storage/ergo_http_peers.json\"" "$TARGET_DIR/config.yaml"
  else
    sed -i "s|/nodo|$TARGET_DIR|g" "$TARGET_DIR/config.yaml"
  fi
  chmod a+w "$TARGET_DIR/config.yaml"
fi

# Apply custom architecture-specific setup
if [ "$(uname -m)" = "arm64" ] || [ "$(uname -m)" = "aarch64" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_arm.sh"
elif [ "$(uname -m)" = "x86_64" ]; then
  SETUP_SCRIPT="bash/setup_ubuntu_x86.sh"
else
  exit 1
fi

chmod +x "$SETUP_SCRIPT"

printf "Running setup script $SETUP_SCRIPT...\n"
# We might need sudo for some parts of the setup script (installing packages)
[ "$USE_SUDO" = false ] && export NODO_ROOTLESS=1
if ! ./"$SETUP_SCRIPT" "$TARGET_DIR"; then

  printf "Error: The setup script $SETUP_SCRIPT failed to execute.\n" >&2
  exit 1
fi

# Setup Docker Rootless
chmod +x bash/setup_docker_rootless.sh
./bash/setup_docker_rootless.sh "$TARGET_DIR"

SCRIPT_USER=$(logname 2>/dev/null || echo $USER)

if [ "$USE_SUDO" = true ]; then
    printf "Setting up systemd service...\n"
    chmod +x "$TARGET_DIR/bash/daemon_setup.sh"
    "$TARGET_DIR/bash/daemon_setup.sh" "$TARGET_DIR"
fi

create_wrapper_script() {
  if [ "$USE_SUDO" = true ]; then
      WRAPPER_SCRIPT="/usr/local/bin/nodo"
  else
      mkdir -p "$HOME/.local/bin"
      WRAPPER_SCRIPT="$HOME/.local/bin/nodo"
      
      # Ensure ~/.local/bin is in the shell configuration
      printf "Ensuring $HOME/.local/bin is in your PATH configuration...\n"
      
      ADDED_TO_CONFIG=false
      EXPORT_CMD='export PATH="$HOME/.local/bin:$PATH"'

      # Function to safely append to config
      append_to_config() {
          local cfg="$1"
          local cmd="$2"
          if [ -f "$cfg" ]; then
              # Check if the file already contains the path (simplified check)
              if ! grep -Fq ".local/bin" "$cfg"; then
                  echo "" >> "$cfg"
                  echo "# Added by nodo installer" >> "$cfg"
                  echo "$cmd" >> "$cfg"
                  printf "  - Added to %s\n" "$cfg"
                  ADDED_TO_CONFIG=true
              else
                  printf "  - Already configured in %s (found '.local/bin')\n" "$cfg"
              fi
          fi
      }

      # Try common config files
      # Bash (Interactive)
      append_to_config "$HOME/.bashrc" "$EXPORT_CMD"
      # Bash (Login) / Sh
      append_to_config "$HOME/.bash_profile" "$EXPORT_CMD"
      append_to_config "$HOME/.profile" "$EXPORT_CMD"
      # Zsh
      append_to_config "$HOME/.zshrc" "$EXPORT_CMD"

      # Fish detection
      # Fish detection
      case "$SHELL" in
        */fish)
          printf "  - Detected Fish shell.\n"
          printf "    Please run: set -U fish_user_paths \$HOME/.local/bin \$fish_user_paths\n"
          ;;
      esac

      if [ "$ADDED_TO_CONFIG" = true ]; then
          printf "Successfully updated shell configuration.\n"
      fi
      
      # Check if ~/.local/bin is in the CURRENT PATH
      case ":$PATH:" in
          *":$HOME/.local/bin:"*) ;;
          *)
              # If we are in an interactive shell, we can try to reload it
              case "$-" in
                  *i*)
                      printf "\nConfiguration updated. Reloading shell to apply changes...\n"
                      printf "********** You can now use the 'nodo' command. **********\n"
                      exec "$SHELL"
                      ;;
                  *)
                      printf "\nWARNING: $HOME/.local/bin is NOT in your current PATH.\n"
                      printf "To use the 'nodo' command immediately, run:\n"
                      printf "  source ~/.bashrc\n"
                      ;;
              esac
              ;;
      esac
  fi


  # Check if the wrapper script already exists and remove it
  if [ -f "$WRAPPER_SCRIPT" ]; then
    printf "Wrapper script %s already exists. Removing it...\n" "$WRAPPER_SCRIPT"
    [ "$USE_SUDO" = true ] && sudo rm -f "$WRAPPER_SCRIPT" || rm -f "$WRAPPER_SCRIPT"
  fi

  printf "Creating %s...\n" "$WRAPPER_SCRIPT"

  # Create the wrapper script.
  cat <<EOF > "nodo_wrapper.sh"
#!/bin/bash
# ORIGINAL_DIR is set to the current directory at runtime
ORIGINAL_DIR="\$PWD"
# Change directory to TARGET_DIR, which was expanded at compile time
cd "$TARGET_DIR" || exit

# Start Docker Rootless if needed
./bash/run_docker_rootless.sh "$TARGET_DIR"
export DOCKER_HOST="unix://$TARGET_DIR/docker/docker.sock"

# Activate the virtual environment in TARGET_DIR
source "$TARGET_DIR/venv/bin/activate"
# Execute the Python script with the runtime ORIGINAL_DIR and passed arguments
ORIGINAL_DIR="\$ORIGINAL_DIR" python3 "$TARGET_DIR/nodo.py" "\$@"
EOF

  if [ "$USE_SUDO" = true ]; then
      sudo mv nodo_wrapper.sh "$WRAPPER_SCRIPT"
      sudo chmod +x "$WRAPPER_SCRIPT"
  else
      mv nodo_wrapper.sh "$WRAPPER_SCRIPT"
      chmod +x "$WRAPPER_SCRIPT"
  fi

  printf "Setting permissions for %s...\n" "$TARGET_DIR"
  [ "$USE_SUDO" = true ] && sudo chmod -R 777 "$TARGET_DIR" || chmod -R 775 "$TARGET_DIR"
}

create_wrapper_script

# Permissions and service restart are handled by daemon_setup.sh if USE_SUDO is true.


ACCEPT_KYA_SCRIPT="bash/accept_kya.sh"
chmod +x "$ACCEPT_KYA_SCRIPT"

printf "Installation and setup completed successfully. The repository is located at $TARGET_DIR.\n"
printf "********** You can now use the 'nodo' command. **********\n"