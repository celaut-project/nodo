
# Nodo: User Guide

This guide is designed to help you understand and use the basic commands available in Nodo, a service orchestration tool for distributed networks. Below are the commands that are most relevant to basic users, including how to execute services, connect to peers, configure the system, and more.

## Basic Commands Overview

Here is a list of the most commonly used commands in Nodo for a basic user:

- `execute <service id>`: Executes a service instance based on the specified service ID.
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

### 3. config

The `config` command is used to configure various environment variables related to Nodo's operation. This includes network URLs, wallet mnemonics, and optional donation settings.

**Usage Example:**
`nodo config`
This will initiate an interactive session to guide you through configuring your system.

### 4. compile

The `compile` command allows you to compile a project (such as a Dockerfile and associated configuration files) and create a service specification. This service can then be deployed and run on the distributed network.

**Usage Example:**
`nodo compile /path/to/project`
This will compile the project located in `/path/to/project`.

### 5. tui

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
- `storage:prune_blocks`: Removes unnecessary blocks from the storage to optimize performance.
- `test <test name>`: Executes tests for specific services or features.

## Getting Help

If you ever need assistance with commands or usage, you can run `nodo` without arguments to get a list of available commands:
`nodo`

This will print the following:

- `execute <service id>`
- `connect <ip:url>`
- `serve`
- `config`
- `migrate`
- `storage:prune_blocks`
- `test <test name>`
- `compile <project directory>`
- `tui`

For more detailed questions, please ask on ![Discord](https://discord.com/channels/668903786361651200/1242433742446788649)
