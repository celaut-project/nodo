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

Basic installation:
```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo sh
```

### Note on Installation

- **Requirements**: Needs Ubuntu 22.04.5 LTS and curl package installed.

- **Version**: The Nodo version is currently in 'alpha', so it's recommended to use a virtual machine.

- **Sudo Usage**: The installation script requires `sudo` privileges to install various apt packages and Docker. Use it responsibly under your own discretion.

- **Installation without sudo**: For a manual installation without directly executing the script with sudo, please follow our [manual guide](docs/NoSUDO.md).

- **Docker Containers**: The system will create and remove Docker containers as part of its operations.


## Platform Compatibility

Below is a breakdown of **Nodo** feature support across different operating systems. Since the project is still under development, most capabilities are currently available only on Linux, with varying levels of maturity.

| Functionality         | Linux    | Mac              | Windows          |
| --------------------- | -------- | ---------------- | ---------------- |
| Local execution       | 🟢 Beta  | 🔴 Not supported | 🔴 Not supported |
| Packaging             | 🟢 Beta  | 🔴 Not supported | 🔴 Not supported |
| Local network         | 🟡 Alpha | 🔴 Not supported | 🔴 Not supported |
| Trustless network     | 🟡 Alpha | 🔴 Not supported | 🔴 Not supported |

* 🟢 **Beta**: Functionality implemented and relatively stable.
* 🟡 **Alpha**: Functionality under active development and subject to change.
* 🔴 **Not supported**: Functionality not available on this platform.


## Usage

For detailed usage instructions, please refer to the [User Guide](docs/USAGE.md).


## Know Your Assumptions

Before using **Nodo**, it's essential to understand the assumptions and risks involved. Please review the [Know Your Assumptions (KyA)](docs/KyA.md)
document to ensure you are fully aware of your responsibilities and the limitations of the software.


## About trust between peers

All payments, reputation submissions, and service remunerations are handled decentralized on the Ergo blockchain.
Check how and why Nodo uses [Ergo](docs/ERGO.md).
