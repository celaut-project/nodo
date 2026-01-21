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

- **instances**  
  Lists all running instances and their details.  
  **Example:**  
  `nodo instances`

- **instances --grouped**  
  Lists running instances grouped by their parent service.  
  **Example:**  
  `nodo instances --grouped`

---

## Additional Commands

These commands offer extended management and exploration features:

- **inspect `<service id | tag>`**  
  Inspects details of a specific service.  
  **Example:**  
  `nodo inspect 1234567890abcdef`

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

## Development Commands

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

- **ggconf `<repository path>`**  
  "generate_gateway_config_dev"
 Generates the files needed to run the specified repository locally.
  **Example:**  
  `nodo ggconf /path/to/repository`

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

- **docker `<docker args>`**  
  Executes Docker commands in nodo's isolated Docker context. This allows you to inspect containers, images, and other Docker resources that belong to nodo without seeing your personal Docker environment.  
  **Examples:**  
  ```bash
  nodo docker ps                    # List nodo's running containers
  nodo docker images                # List nodo's Docker images
  nodo docker logs <container_id>   # View logs of a nodo container
  nodo docker stats                 # View resource usage of nodo containers
  ```

---

## Isolated Docker Environment

Nodo uses an **isolated Docker daemon** that is completely separate from your personal Docker environment. This means:

- **Containers created by nodo** are not visible when you run `docker ps` on your system
- **Your personal containers** are not visible to nodo
- **Images and volumes** are stored separately in `{MAIN_DIR}/docker/data`

### Why Isolated Docker?

1. **Clean separation**: Your development containers won't interfere with nodo's service containers
2. **Security**: Nodo's operations are sandboxed from your personal Docker environment
3. **Easy cleanup**: Uninstalling nodo removes all its Docker resources without affecting your personal containers

### Accessing Nodo's Docker

Use the `nodo docker` command to interact with nodo's isolated Docker daemon:

```bash
# Instead of:
docker ps

# Use:
nodo docker ps
```

The isolated Docker daemon configuration is stored in `{MAIN_DIR}/docker/config/daemon.json`.

---

## Daemon execution

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
