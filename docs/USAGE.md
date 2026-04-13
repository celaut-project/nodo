## Nodo: User Guide

This guide will help you understand and use the available commands in **Nodo**, a service orchestration tool for distributed networks. Below is a complete list of commands along with usage examples.

---

## Basic Commands

These are the most commonly used commands for daily tasks:

- **execute `[--remote] [-e key value] <service id | service tag | '.celaut' file path>`**  
  Launches a service instance. Use `--remote` to advertise the host-facing IP instead of the internal VM/container IP. Use `-e` to add service enviroment variables.  
  **Example:**  
  `nodo execute 1234567890abcdef`
  `nodo execute --remote 1234567890abcdef`
  `nodo execute --remote -e workers 8 -e timeout 20 1234567890abcdef`

- **estimate `<service id | service tag | '.celaut' file path>`**  
  Estimates service execution cost without launching it.  
  Prints:
  - execution feasibility (`YES/NO`)
  - reason when execution is not possible
  - estimated gas costs (initial and maintenance)
  - resource availability and total capacity (CPU, RAM, disk)
  
  **Examples:**  
  `nodo estimate 1234567890abcdef`  
  `nodo estimate my_service_tag`  
  `nodo estimate ./my-service.celaut`

- **remove `<service id>`**  
  Removes a service from the node using its ID.  
  **Example:**  
  `nodo remove 1234567890abcdef`

- **kill `<instance id>`**  
  Stops a running service instance by ID.  
  **Example:**  
  `nodo kill abcdef1234567890`

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

- **publish `<service id | service tag>`**  
  Exports a local service and publishes it in chunks to the configured GitHub repository.
  **Examples:**  
  `nodo publish 1234567890abcdef`  
  `nodo publish my_service_tag`

- **download `<manifest url>`**  
  Downloads a published service from a manifest URL, verifies integrity, and imports it locally.
  **Examples:**  
  `nodo download https://raw.githubusercontent.com/user/repo/main/uploads/<service_hash>/manifest`  
  `nodo download https://raw.githubusercontent.com/user/repo/main/uploads/<service_hash>/manifest -o /tmp/services`

- **integrity `[<service id | service tag>] [--fix]`**  
  Verifies registry/metadata integrity for all services or a specific one.
  Use `--fix` to repair detected inconsistencies.
  **Examples:**  
  `nodo integrity`  
  `nodo integrity my_service_tag --fix`

- **instances**  
  Lists all running instances and their details.  
  **Example:**  
  `nodo instances`

- **instances --grouped**  
  Lists running instances grouped by their parent service.  
  **Example:**  
  `nodo instances --grouped`

### Hash Configuration

Service/file identification uses `hashing.HASH` from `config.yaml`.
It accepts aliases (`sha3_256`, `sha256`, `shake_256`, `blake2b`) or a hash-id in hex.

```yaml
hashing:
  HASH: "sha3_256"
  CHECK_INTEGRITY_ON_SERVE: false
```

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

## Estimate Resource Calculation Notes

`nodo estimate` uses the same internal checks as runtime cost estimation:

- **Execution feasibility check**
  - Uses the service `resources.at_most.mem_limit`.
  - Validation uses the same memory guard as execution flow (`could_ve_this_sysreq`).

- **Service memory pool**
  - Total/available pool comes from `IOBigData`, which is initialized with:
  `virtual_memory().available`
  - This represents memory reserved for service execution decisions in nodo.

- **System totals**
  - CPU total: physical cores via `psutil.cpu_count(logical=False)`
  - CPU available: `100 - psutil.cpu_percent(...)`
  - RAM total/available: `psutil.virtual_memory().total` / `.available`
  - Disk total/free: `psutil.disk_usage('/').total` / `.free`

---

## Development Commands

These are intended for development or advanced maintenance environments:

- **update**  
  Updates Nodo (requires superuser privileges).  
  **Example:**  
  `sudo nodo update`

- **serve**  
  Starts Nodo daemon. If already running in the background, an alert will be shown.  
  **Example:**  
  `nodo serve`

- **daemon `<subcommand>`**  
  Manages the Nodo systemd service (requires superuser privileges).  
  Subcommands: start, status, stop, restart  
  **Examples:**  
  `sudo nodo daemon start`  
  `sudo nodo daemon status`  
  `sudo nodo daemon stop`  
  `sudo nodo daemon restart`

- **doctor**  
  Checks and fixes the Nodo systemd service configuration, and performs comprehensive virtualization and Cloud Hypervisor compatibility checks (requires superuser privileges).  
  Checks performed:
  - Systemd service file integrity
  - CPU virtualization flags (vmx/svm)
  - KVM kernel modules and /dev/kvm access
  - Cloud Hypervisor binary existence and version
  - Host kernel version (warns about bleeding-edge kernels with KVM incompatibilities)
  - Guest kernel (`vmlinuz`) presence and size validation
  - Custom initramfs presence and required entry validation
  - **KVM smoke test**: launches a minimal VM to verify that the Cloud Hypervisor binary can actually execute vCPUs on the host kernel  
  **Example:**  
  `sudo nodo doctor`

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
Binary paths for Java/Python/yq/Docker can be overridden in `config.yaml` under `dependencies.*`.

If `buildx` builds inside nodo's isolated Docker can't resolve or reach external hosts (e.g. `github.com`), check that file for a forced `dns` setting that doesn't work in your network (common in corporate/VPN environments). After changing it, restart `nodo.service` (or rerun `{MAIN_DIR}/bash/start_docker_daemon.sh {MAIN_DIR}`).

Cross-architecture builds are intentionally disabled in the default installer profile (no QEMU/binfmt provisioning). If your service is `linux/arm64`, build from an `arm64` host; if it is `linux/amd64`, build from an `amd64` host.

### Service Container Security Options

Nodo applies Docker `security_opt` values when creating service containers:

- `seccomp={MAIN_DIR}/src/virtualizers/docker/seccomp.json`
- `apparmor=unconfined` (when AppArmor is enabled on the host)
- `label=disable` (when SELinux is enabled on the host)

You can tune these options in `config.yaml` under:

- `docker.SECURITY_APPARMOR_UNCONFINED`
- `docker.SECURITY_SELINUX_DISABLE_LABEL`

---

## Daemon execution

### Automatic Execution via systemd

If Nodo was installed with superuser privileges, it will be automatically configured as a `systemd` service to run in the background.

### Managing the Service

Use `nodo daemon` commands to start, stop, restart, or check the status of the Nodo service:

- `sudo nodo daemon start` - Start the service
- `sudo nodo daemon stop` - Stop the service  
- `sudo nodo daemon restart` - Restart the service
- `sudo nodo daemon status` - Check service status

Use `sudo nodo doctor` to check and fix the service configuration if issues arise.

### Manual Execution in Development Mode: `nodo serve`

Use `nodo serve` to run Nodo in a development environment or when you don’t want to use background service mode.
If `hashing.CHECK_INTEGRITY_ON_SERVE` is set to `true`, Nodo runs an automatic integrity/migration check before starting.

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
