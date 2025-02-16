# Nodo: User Guide

This guide is designed to help you understand and use the basic commands available in Nodo, a service orchestration tool for distributed networks. Below are the commands that are most relevant to basic users, including how to execute services, connect to peers, configure the system, and more.

## Basic Commands Overview

Here is a list of the most commonly used commands in Nodo for a basic user:

- `execute <service id>`: Executes a service instance based on the specified service ID.
- `remove <service id>`: Removes a service based on the specified service ID from the node.
- `stop <instance id>`: Stops a service instance based on the specified instance ID.
- `increase_gas <instance id> <gas to add>`: Increases the gas allocated to a service instance.
- `decrease_gas <instance id> <gas to retire>`: Decreases the gas allocated to a service instance.
- `services`: List all the services currently available on the node.
- `connect <ip:url>`: Manually connects to a peer node by specifying its IP and port.
- `pack <project directory>`: Compiles a project to create a service for the Celaut network.
- `config`: Configures various settings related to the network, wallets, and optional donation setup.
- `tui`: Launches a terminal user interface (TUI) to visualize peers, services, and instances.
- `info`: Provides details about the Nodo service status, version, and configuration.
- `logs`: Displays the application log for real-time monitoring.
- `export <service> <path>`: Exports a service to a specified path.
- `import <path>`: Imports a service from a specified path.

## Command Details

### 1. execute

This command is used to run a specific service instance on the network. The service ID refers to the unique identifier of the service you want to execute.

**Usage Example:**
`nodo execute 1234567890abcdef`
This will execute the service with the ID 1234567890abcdef.

### 2. connect

This command allows you to manually connect to a peer node in the distributed network by specifying its IP and port. Useful if automatic peer discovery isn't sufficient.

**Usage Example:**
`nodo connect 192.168.1.10:4040`
In this example, the node will attempt to connect to a peer located at IP 192.168.1.10 on port 4040.

### 3. remove

This command deletes a specific service from the node using its service ID. When you use this command, it permanently removes the specified service.

**Usage Example:**
`nodo remove 1234567890abcdef`
This will remove the service with the ID 1234567890abcdef from the node.

### 4. stop

Stops a running service instance using its instance ID. Useful for freeing resources or terminating unnecessary services.

**Usage Example:**
`nodo stop abcdef1234567890`
This will stop the service instance with the ID abcdef1234567890.

### 5. increase_gas / decrease_gas

These commands adjust the gas allocated to a service instance.

**Usage Examples:**
- Increase Gas: `nodo increase_gas abcdef1234567890 100`
- Decrease Gas: `nodo decrease_gas abcdef1234567890 50`

### 6. services

Lists all services currently available on the node, along with their IDs and other details.

**Usage Example:**
`nodo services`
This will display a list of all services available on the node.

### 7. config

Configures various environment variables related to Nodo's operation, including network URLs, wallets, etc.

**Usage Example:**
`nodo config`
This will initiate an interactive session for system configuration.

#### Modifiable Fields

- **ERGO_NODE_URL**: Connects your node to the Ergo network.
- **ERGO_WALLET_MNEMONIC**: Generates reputation proofs and sends payments.
- **ERGO_PAYMENTS_RECEIVER_WALLET**: Where funds exceeding a threshold are sent.
- **NGROK_TUNNELS_KEY**: Exposes the node to the internet via NGROK.
- **ERGO_DONATION_PERCENTAGE**: Opt-in donation percentage to support Nodo.

### 8. pack

Compiles a project to create a service specification for deployment on the network.

**Usage Example:**
`nodo pack /path/to/project`
This will pack the project located in `/path/to/project`.

For the compilation to succeed, ensure that a `Dockerfile` and a [`service.json`](../src/packers/README.md) file are present either in the root directory or within a `.service` folder of the project.  

For a deeper understanding of the compilation command, refer to the [Compilation Command Guide](../src/commands/pack/zip_with_dockerfile/README.md).

### 9. tui

Launches the terminal-based user interface to visualize and interact with the network.

**Usage Example:**
`nodo tui`
Monitor your node, view services, and instances directly from the terminal.

### 10. info

Provides information about the Nodo service status, version, and configuration.

**Usage Example:**
`nodo info`
Displays current status, version, IP address, etc.

### 11. logs

Displays real-time logs for monitoring.

**Usage Example:**
`nodo logs`
Continuously show logs of application activity.

### 12. export / import

Exports or imports a service, facilitating data or service transfers.

**Usage Examples:**
- Export: `nodo export MyService /path/to/export`
- Import: `nodo import /path/to/service`

## Important Note on Service Management

### Automatic Service Management via systemd

Nodo automatically starts as a `systemd` daemon if installed with sudo. This ensures background operation without manual intervention.

### Manual Service Execution: nodo serve

Use `nodo serve` for manual or development environments.

**Usage Example:**
`nodo serve`

## TUI Interface

The TUI interface provides a graphical way to monitor the network and services through the terminal. The sections include Peers, Clients, Instances, Services, Envs, and Tunnels.

### Key Commands for TUI

- Left/Right arrow keys: Navigate sections.
- Up/Down arrow keys: Move rows within a section.
- 'o' and 'p': Rotate views within a section.
- 'm': Change block view layout.
- 'c': Connect to a peer directly.

## Advanced Commands

For users exploring deeper functionality:

- `serve`: Initiates Nodo service, recommended for development.
- `migrate`: Updates database schema.
- `storage:prune_blocks`: Reduces disk usage by removing unnecessary blocks.
- `test <test name>`: Executes tests for specific services or features.
- `rundev <repository path>`: Run development version from specified repo.
- `submit_reputation`: Force submits reputation info.
- `refresh_ergo_nodes`: Refresh Ergo nodes. One of them will be used as Ergo node provider.
- `daemon`: Starts Nodo in daemon mode.

## Getting Help

Run `nodo` without arguments to see available commands:

```
- execute <service id>
- remove <service id>
- stop <instance id>
- increase_gas <instance id> <gas to add>
- decrease_gas <instance id> <gas to retire>
- services
- connect <ip:url>
- pack <project directory>
- config
- tui
- info
- logs
- export <service> <path>
- import <path>
- serve
- migrate
- storage:prune_blocks
- test <test name>
- rundev <repository path>
- submit_reputation
- daemon
```

For more details, join us on [Discord](https://discord.com/channels/668903786361651200/1242433742446788649)