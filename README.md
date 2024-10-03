# Nodo: Service Orchestration for Distributed Networks

Nodo is a powerful framework designed to streamline communication, management, and orchestration of services
across a network of computers.

In a network where
services are specialized software components encapsulated within binary files, and nodes represent the computers in
the network responsible for discovering and establishing connections with other nodes
(aka [CELAUT](https://github.com/celaut-project/celaut-architecture/blob/master/README.md)).

As is described in the [architecture](https://github.com/celaut-project/celaut-architecture/blob/master/README.md#node-responsabilities),
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

5. **Service Compilation**: Although it is not necessarily a CELAUT node's responsibility, this implementation allows you to send
a Dockerfile along with a configuration file and a zip file and get a specification for that service, making it a hassle-free process for users (or bots ...).


## Installation

```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/master/install.sh | sudo sh
```

### Note on Installation

- **version**: The Nodo version is currently in 'alpha', so it's recommended to use a virtual machine.

- **sudo Usage**: The installation script requires `sudo` privileges to install various apt packages and Docker. Use it responsibly under your own discretion.

- **Docker Containers**: The system will create and remove Docker containers as part of its operations.


## Usage

For detailed usage instructions, please refer to the [User Guide](docs/USAGE.md).


## Know Your Assumptions

Before using **Nodo**, it's essential to understand the assumptions and risks involved. Please review the [Know Your Assumptions (KyA)](docs/KyA.md)
document to ensure you are fully aware of your responsibilities and the limitations of the software.
