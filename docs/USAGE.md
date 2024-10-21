
# Nodo: User Guide

This guide is designed to help you understand and use the basic commands available in Nodo, a service orchestration tool for distributed networks. Below are the commands that are most relevant to basic users, including how to execute services, connect to peers, configure the system, and more.

## Basic Commands Overview

Here is a list of the most commonly used commands in Nodo for a basic user:

- `execute <service id>`: Executes a service instance based on the specified service ID.
- `remove <service id>`: Removes a service based on the specified service ID from the node.
- `stop <instance id>`: Removes a service instance based on the specified instance ID.
- `services`: List all the services currently available on the node.
- `connect <ip:url>`: Manually connects to a peer node by specifying its IP and port.
- `config`: Configures various settings related to the network, wallets, and optional donation setup.
- `compile <project directory>`: Compiles a project (e.g., a Dockerfile and associated configuration) to create a service that can be executed within the Celaut network.
- `tui`: Launches a terminal user interface (TUI) to visualize peers, services, and instances.

## Command Details

### 1. execute

This command is used to run a specific service instance on the network. The service ID refers to the unique identifier of the service you want to execute.

**Usage Example:**
`nodo execute 1234567890abcdef`
This will execute the service with the ID 1234567890abcdef.

### 2. connect

This command allows you to manually connect to a peer node in the distributed network by specifying its IP and port. This is useful if automatic peer discovery isn't sufficient or if you need to establish a specific connection.

**Usage Example:**
`nodo connect 192.168.1.10:4040`
In this example, the node will attempt to connect to a peer located at IP 192.168.1.10 on port 4040.

### 3. remove

This command is used to delete a specific service from the node using its service ID. When you use this command, it permanently removes the specified service, making it unavailable for further execution until it is recompiled or added back.

**Usage Example:**
`nodo remove 1234567890abcdef`
This will remove the service with the ID 1234567890abcdef from the node.

### 4. stop

The stop command is used to halt a running service instance using its instance ID. This is useful when you want to free up resources or terminate a service that is
no longer needed.

**Usage Example:**
`nodo stop abcdef1234567890`
This will stop the service instance with the ID abcdef1234567890.

### 5. services

The services command is used to list all the services currently available on the node. This includes services that have been compiled or added and are ready for
execution. It helps users see what services are at their disposal, along with their respective service IDs.

**Usage Example:**
`nodo services`
This will display a list of all services available on the node, including their names, IDs, and other relevant details.

### 6. config

The `config` command is used to configure various environment variables related to Nodo's operation. This includes network URLs, wallet mnemonics, and optional donation settings.

**Usage Example:**
`nodo config`
This will initiate an interactive session to guide you through configuring your system.

#### Modifiable Fields

1. **ERGO_NODE_URL**:
   This is the URL of the Ergo node, which is the default blockchain network used for both payment systems and the reputation system. It connects your node to the Ergo network, enabling it to interact with the blockchain.

2. **ERGO_WALLET_MNEMONIC**:
   This is the main wallet for your node. It is used for generating reputation proofs and for sending payments to other nodes. A random wallet is generated when the node is first installed, but the user can provide a different wallet mnemonic if they prefer.

3. **ERGO_PAYMENTS_RECIEVER_WALLET**:
   This is the public key where funds should be sent once they exceed a certain threshold. It is recommended to use a cold wallet (offline wallet) for additional security.

4. **NGROK_TUNNELS_KEY**:
   If your node doesn't have a public IP available for internet exposure, you can use a service like NGROK. This key allows the node to expose itself to the internet through NGROK tunnels.

5. **ERGO_DONATION_PERCENTAGE**:
   Users can opt to donate a percentage of their income (earned from clients) to support the development of Nodo. The donation percentage can be set by the user, with 0% being the default value, as the repository `celaut-proyect/nodo` operates as a non-profit. The donation destination can be viewed in the environment variables.

**Check how and why Nodo uses [Ergo](ERGO.md).**

### 7. compile

The `compile` command allows you to compile a project (such as a Dockerfile and associated configuration files) and create a service specification. This service can then be deployed and run on the distributed network.

**Usage Example:**
`nodo compile /path/to/project`
This will compile the project located in `/path/to/project`.

### 8. tui

The `tui` command launches the terminal-based user interface, allowing you to visualize and interact with the network in real-time. You can view peer connections, service instances, and other important details about the network's operation.

**Usage Example:**
`nodo tui`
Once the TUI is running, you can monitor your node, view services and instances directly from the terminal.

## Important Note on Service Management

### Automatic Service Management via systemd

After installation, if the installation was performed with sudo (as currently required), Nodo will automatically start a `systemd` daemon to manage the main process. This ensures that Nodo runs as a background service without requiring any manual intervention from the user after installation. The daemon handles the orchestration of the services and peers across the network in the background.

### Manual Service Execution: nodo serve

Currently, Nodo can only be installed using superuser privileges (sudo). As a result, the command `nodo serve` should only be used for development purposes or in situations where systemd is not managing the service.

If you did not install Nodo with sudo, or are using it in a development environment, you will need to manually start the service using:
`nodo serve`


## TUI Interface

The TUI interface provides a graphical way to monitor the network and services through the terminal. The image below demonstrates the layout and structure of the interface, with a top menu for navigating different sections like Peers, Clients, Instances, Services, Envs, and Tunnels.

### Key Sections of the TUI

- **Peers**: Displays information about the peers to which your node is currently connected. Shows details such as the peer ID, the main URI, and the status of any gas (or resources) on that peer.

- **Clients**: View the clients interacting with your node. These clients may request the execution of services, and they can be other nodes or external applications.

- **Instances**: Lists the currently running service instances on your node, including their ID and status.

- **Services**: Displays all the services installed locally on your node, showing which services are available for execution.

- **Envs**: Displays the environment variables currently configured for your node, including settings for wallets and configurations.

- **Tunnels**: Enables nodes without a public IP address to connect to the internet using providers like NGROK. Essential for nodes behind NAT or firewalls.

### Key Commands for TUI

- Left/Right arrow keys: Navigate between sections.
- Up/Down arrow keys: Move through rows within a section.
- 'o' and 'p': Rotate between different views within a section.
- 'm': Change the layout of the block view.
- 'c': Connect to a new peer directly from the TUI.

## Advanced Commands

For users looking to explore deeper functionality, the following advanced commands are available:

- `serve`: Initiates the Nodo service, recommended for development purposes.
- `migrate`: Runs database migrations to update the schema.
- `storage:prune_blocks`: Removes unnecessary blocks from the storage to reduce disk usage.
- `test <test name>`: Executes tests for specific services or features.

## Getting Help

If you ever need assistance with commands or usage, you can run `nodo` without arguments to get a list of available commands:
`nodo`

This will print the following:

- `execute <service id>`
- `remove <service id>`
- `stop <instance id>`
- `services`
- `connect <ip:url>`
- `serve`
- `config`
- `migrate`
- `storage:prune_blocks`
- `test <test name>`
- `compile <project directory>`
- `tui`

For more detailed questions, please ask on [Discord](https://discord.com/channels/668903786361651200/1242433742446788649)
