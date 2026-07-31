# Nodo: A Celaut node implementation

Nodo is a powerful framework designed to streamline communication, management, and orchestration of services
across a network of computers.

In a network where
services are specialized software components encapsulated within binary files, and nodes represent the computers in
the network responsible for discovering and establishing connections with other nodes
(aka [CELAUT](https://github.com/celaut-project/paradigm/blob/master/README.md)).

As is described in the [paradigm repository](https://github.com/celaut-project/paradigm/blob/master/README.md#node-responsabilities),
it's responsibilities are:

1. **Service Execution**: Handles service instance requests, balancing the load between running them
locally or on its peer nodes. This ensures an efficient distribution of tasks and resources across the network,
optimizing system performance.

2. **Communication Interface**: Provides a robust and flexible interface that enables the services that it executes
to communicate seamlessly with it, ensuring efficient data exchange and coordination.

3. **Address and Token Provisioning**: Offers a streamlined process for obtaining the communication address and
authentication token of a service required for interaction, enhancing security and accessibility.

4. **Dependency Management**: Takes care of ensuring that services have access to the addresses of their
dependencies, irrespective of the node on which they are executed, promoting a smooth and efficient service ecosystem.

5. **Service Packing**: Although it is not necessarily a Celaut node's responsibility, this implementation allows you to send
a Dockerfile along with a configuration file and get a specification for that service, making it a hassle-free process for users.


## Installation

### Linux

Basic installation:
```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo bash
```

<details>
<summary>Linux installation notes</summary>

- **Requirements**: `git`, `curl`, `sudo`, `iptables`, `bc`.

- **Supported distributions**: Nodo has only been tested on Debian-based distributions so far.

- **Sudo Usage**: The installation script requires `sudo` privileges for system-level setup. Python, Java, and `yq` runtimes are installed locally under `MAIN_DIR`.

- **Installation without sudo**: For a manual installation without directly executing the script with sudo, please follow the [manual guide](docs/INSTALL.md).

- **Configurable binary paths**: Java, Python, and `yq` binaries can be customized in `config.yaml` under `dependencies.*`.
  
> Future versions of Nodo aim to progressively reduce the number of external system dependencies.

</details>

### Windows 11

Windows users should download and run the official installer:

[Nodo Windows Installer (.exe)](https://github.com/celaut-project/nodo/releases/download/v1/Nodo-Setup.exe)

<details>
<summary>Windows installation details</summary>

The installer automatically creates an isolated Linux distribution dedicated to **Nodo**, allowing the node to run separated from the rest of the operating system and user environment.

No manual Linux environment setup is required.

</details>

### Development

<details>
<summary>Developer installation (click to expand)</summary>

Local source installation (development workflow):
```bash
cd /home/user/Desktop/nodo
sudo ./install.sh --source-dir /home/user/Desktop/nodo
```

This mode keeps `nodo.service` pointed to your local checkout, so code changes can be tested by restarting the service (no push/reinstall cycle needed).

Branch-based installation (default branch is `stable`):
```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/dev/install.sh | sudo bash -s -- --branch dev
```

</details>


## Platform Compatibility

Below is a breakdown of **Nodo** feature support across different operating systems. Since the project is still under development, capability levels vary by platform.

| Functionality         | Linux     | Windows          | Mac              |
|---------------------- |---------- |------------------|------------------|
| Local execution       | 🟢 Beta  | 🟢 Beta          | 🔴 Not supported |
| Packaging             | 🟢 Beta  | 🟢 Beta          | 🔴 Not supported |
| Local network         | 🟡 Alpha | 🟡 Alpha         | 🔴 Not supported |
| Trustless network     | 🟡 Alpha | 🟡 Alpha         | 🔴 Not supported |

* 🟢 **Beta**: Functionality implemented and relatively stable.
* 🟡 **Alpha**: Functionality under active development and subject to change.
* 🔴 **Not supported**: Functionality not available on this platform.


## Usage

For detailed usage instructions, please refer to the [User Guide](docs/USAGE.md).

To package your own project into a Celaut service, see the [Packing Guide](docs/PACKING.md).
To remove Nodo, see the [Uninstallation Guide](docs/UNINSTALL.md).


## Know Your Assumptions

Before using **Nodo**, it's essential to understand the assumptions and risks involved. Please review the [Know Your Assumptions (KyA)](docs/KyA.md)
document to ensure you are fully aware of your responsibilities and the limitations of the software.


## About trust between peers

All payments, reputation submissions, and service remunerations are handled decentralized on the Ergo blockchain.
Check how and why Nodo uses [Ergo](docs/ERGO.md).
