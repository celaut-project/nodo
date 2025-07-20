## **Manual Installation Guide for the Node**

This guide will walk you through the node's installation process. It is divided into two main phases:

1.  **Administrator Phase:** Steps requiring `sudo` privileges to prepare your system.
2.  **User Phase:** Steps you can run as a regular user in your home directory, without `sudo`.

-----

### **1. Administrator Phase (requires `sudo`)**

These commands must be executed by a user with superuser privileges to install all the necessary system-wide dependencies and tools.

#### **1.1. Install System and Build Dependencies**

First, you need to update your package repositories and install build tools, required libraries, and Git.

```bash
sudo apt-get update
sudo apt-get install -y git build-essential zlib1g-dev libncurses5-dev \
libgdbm-dev libnss3-dev protobuf-compiler libssl-dev libreadline-dev \
libffi-dev libsqlite3-dev wget libbz2-dev ca-certificates curl gnupg \
lsb-release
```

*(This action consolidates the dependency installation from the setup scripts).*

#### **1.2. Install `yq` (YAML Processor)**

Next, download `yq` and make it executable for all users on the system.

```bash
sudo wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq
sudo chmod +x /usr/local/bin/yq
```

#### **1.3. Install Python 3.11**

You will add the `deadsnakes` PPA to get recent Python versions and then install `python3.11`.

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt-get update
sudo apt-get -y install python3.11 python3.11-venv python3.11-distutils
```

#### **1.4. Install OpenJDK 21**

This step installs the Java 21 runtime environment.

```bash
sudo apt-get -y install openjdk-21-jre-headless
```

#### **1.5. Install Docker and QEMU**

Now, you will set up the official Docker repository, install the Docker Engine, and add multi-architecture support with QEMU.

```bash
# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

# Add the Docker repository to your APT sources
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

# Install Docker Engine (version 24.* as in the script) and QEMU
sudo apt-get -y --allow-downgrades install docker-ce=5:24.* docker-ce-cli=5:24.* containerd.io
sudo apt-get -y install qemu-system binfmt-support qemu-user-static

# Configure QEMU
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

Once these administrator steps are complete, the system is ready for you to set up the node.

-----

### **2. User Phase (no `sudo`)**

You can now perform the following steps in your personal home directory (`$HOME`).

#### **2.1. Clone the Repository**

Open your terminal and clone the project from GitHub into a folder named `nodo` inside your home directory.

```bash
cd ~
git clone https://github.com/celaut-project/nodo.git
cd nodo
```

#### **2.2. Create the Configuration File**

If the configuration file does not exist, create it by copying the example file.

```bash
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
fi
```

#### **2.3. Set Up the Python Virtual Environment**

Create a dedicated virtual environment to install the Python dependencies in isolation, and then activate it.

```bash
python3.11 -m venv venv
source venv/bin/activate
```

#### **2.4. Install `pip` and Python Dependencies**

Ensure `pip` is installed in your virtual environment and then install the packages listed in the `requirements.txt` file.

```bash
wget https://bootstrap.pypa.io/get-pip.py -O get-pip.py
sudo python3.11 get-pip.py
rm get-pip.py
python3 -m pip install -r "$TARGET_DIR/bash/requirements.txt"
```

#### **2.5. Run Initialization and Migration Scripts**

Execute the final scripts to initialize the configuration and apply database migrations.

```bash
# Initialization script
sh ./bash/init_x86.sh

# Application migrations
python3.11 nodo.py migrate
```

#### **2.6. Create an Alias for the `nodo` Command (Optional)**

The original installation script creates a global `nodo` command. Since you are not using `sudo`, you can create a personal alias in your terminal's configuration file (e.g., `~/.bashrc` or `~/.zshrc`) for convenience.

Add the following line to the end of your `~/.bashrc`:

```bash
alias nodo="cd $HOME/nodo && source venv/bin/activate && python3 $HOME/nodo/nodo.py"
```

Then, reload your shell's configuration with `source ~/.bashrc`.

#### **2.7. Start the Node**

The original script creates a `systemd` service to run the node as a background daemon. To replicate this manually, you can run the following command from your `~/nodo` directory:

```bash
# Make sure your virtual environment is activated first
source venv/bin/activate

# Run the node as a daemon
python3 nodo.py daemon
```

To keep it running after you close the terminal, you can use `nohup`:

```bash
nohup python3 nodo.py daemon &
```

The installation is now complete\! The node is running under your user account.