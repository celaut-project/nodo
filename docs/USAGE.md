## Nodo: User Guide

This guide will help you understand and use the available commands in **Nodo**, a service orchestration tool for distributed networks. Below is a complete list of commands along with usage examples.

---

## Basic Commands

These are the most commonly used commands for daily tasks:

- **execute `<service id | service tag | '.celaut' file path>`**  
  Launches a service instance.  
  **Example:**  
  `nodo execute 1234567890abcdef`

- **remove `<service id>`**  
  Removes a service from the node using its ID.  
  **Example:**  
  `nodo remove 1234567890abcdef`

- **stop `<instance id>`**  
  Stops a running service instance by ID.  
  **Example:**  
  `nodo stop abcdef1234567890`

- **increase_gas `<instance id> <gas amount>`**  
  Increases the allocated gas for a service instance.  
  **Example:**  
  `nodo increase_gas abcdef1234567890 100`

- **decrease_gas `<instance id> <gas amount>`**  
  Decreases the allocated gas for a service instance.  
  **Example:**  
  `nodo decrease_gas abcdef1234567890 50`

- **services**  
  Lists all available services on the node.  
  **Example:**  
  `nodo services`

- **connect `<ip:port>`**  
  Manually connects to a peer node.  
  **Example:**  
  `nodo connect 192.168.1.10:4040`

- **pack `<project directory>`**  
  Packages a project to create a service specification.  
  **Example:**  
  `nodo pack /path/to/project`

- **config**  
  Opens environment and runtime configuration options.  
  **Example:**  
  `nodo config`

- **tui**  
  Launches the terminal user interface for monitoring and managing the node.  
  **Example:**  
  `nodo tui`

- **info**  
  Displays service status, version, and configuration details.  
  **Example:**  
  `nodo info`

- **logs**  
  Shows real-time application logs for monitoring.  
  **Example:**  
  `nodo logs`

- **export `<service> <path>`**  
  Exports a service to a specified path.  
  **Example:**  
  `nodo export MyService /export/path`

- **import `<path>`**  
  Imports a service from the specified path.  
  **Example:**  
  `nodo import /service/path`

---

## Additional Commands

These commands offer extended management and exploration features:

- **service `<service id | tag>`**  
  Inspects details of a specific service.  
  **Example:**  
  `nodo service 1234567890abcdef`

- **tag `<service id | tag> <new tag>`**  
  Assigns or updates a tag for a service.  
  **Example:**  
  `nodo tag 1234567890abcdef new_tag`

- **clients**  
  Lists clients currently connected to the node.  
  **Example:**  
  `nodo clients`

- **peers**  
  Displays the list of connected peer nodes.  
  **Example:**  
  `nodo peers`

---

## Advanced Commands

These are intended for development or advanced maintenance environments:

- **update**  
  Updates Nodo (requires superuser privileges).  
  **Example:**  
  `sudo nodo update`

- **serve**  
  Starts Nodo in development mode. If already running in the background, an alert will be shown.  
  **Example:**  
  `nodo serve`

- **migrate**  
  Updates the database schema.  
  **Example:**  
  `nodo migrate`

- **storage:prune_blocks**  
  Cleans up storage by removing unnecessary blocks.  
  **Example:**  
  `nodo storage:prune_blocks`

- **test `<test name>`**  
  Runs a specific test for a service or feature.  
  **Example:**  
  `nodo test test_name`

- **rundev `<repository path>`**  
  Runs a development version of a specified repository.  
  **Example:**  
  `nodo rundev /path/to/repository`

- **submit_reputation**  
  Forces the submission of reputation information.  
  **Example:**  
  `nodo submit_reputation`

- **refresh_ergo_nodes**  
  Refreshes the Ergo nodes list and selects one as a provider.  
  **Example:**  
  `nodo refresh_ergo_nodes`

- **prune_containers**  
  Removes unused containers (requires superuser privileges).  
  **Example:**  
  `sudo nodo prune_containers`

- **daemon**  
  Launches Nodo as a background daemon.  
  **Example:**  
  `nodo daemon`

---

## Important Notes on Service Management

### Automatic Execution via systemd

If Nodo was installed with superuser privileges, it will be automatically configured as a `systemd` service to run in the background.

### Manual Execution in Development Mode: `nodo serve`

Use `nodo serve` to run Nodo in a development environment or when you don’t want to use background service mode.

---

## Terminal User Interface (TUI)

The TUI provides a graphical terminal-based interface for managing nodes and services. Key features:

- **Navigation:**  
  - Left/Right Arrows: Switch sections.  
  - Up/Down Arrows: Move between items.

- **Quick Commands:**  
  - `o` / `p`: Rotate views in a section.  
  - `m`: Toggle block view layout.  
  - `c`: Connect to a peer.

---

## Getting Help

To view a summary of all available commands, simply run:

```bash
nodo
```
