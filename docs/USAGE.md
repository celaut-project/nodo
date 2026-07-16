## Nodo: User Guide

This guide will help you understand and use the available commands in **Nodo**, a service orchestration tool for distributed networks. Below is a complete list of commands along with usage examples.

---

## Non-interactive use (automation / agents) ⚙️

The first time you run Nodo it shows the **Know Your Assumptions (KyA)** document and waits for an interactive `yes/no` acceptance before any command runs. In headless or automated environments (CI, agents, scripts) there is no TTY to answer that prompt.

You can **pre-accept the KyA and skip the gate** by creating an empty marker file at:

```
<MAIN_DIR>/storage/.acceptedkya
```

`MAIN_DIR` is the Nodo main directory configured in `config.yaml` (`main.MAIN_DIR`, default `/nodo`), so by default the marker is:

```bash
mkdir -p /nodo/storage
touch /nodo/storage/.acceptedkya
```

When this file exists, Nodo treats the KyA as already accepted and starts without prompting. This is the same marker the interactive accept flow writes once you answer `yes`.

> ⚠️ Creating this file means you accept the Know Your Assumptions ([`docs/KyA.md`](KyA.md)) without reading the interactive prompt. Only do this in environments you control.

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
  Packages a project into a service. nodo does **not** build locally — it sends
  the project to an external **packer-service** (a microVM that runs
  Docker/buildx in a sealed VM, so Docker is never installed on your host) and
  imports the returned `.celaut.bee`. Configure the packer by its published
  service id first, then `nodo execute` it so a running instance exists:  
  set the packer id under `core_services` in `config.yaml` — the single source of
  truth: `core_services: [{ name: "packer", id: "<packer-service id>" }]`  
  nodo resolves the running instance's `ip:port` automatically. When nodo needs to
  download the packer it uses `packer.PACKER_SOURCE_URL` if set, otherwise the
  source-application core service. To override with an out-of-band packer instead,
  set `packer.PACKER_SERVICE_URL: http://<ip>:8080` in `config.yaml`  
  **Example:**  
  `nodo pack /path/to/project`
  > Check [detailed documentation](../src/commands/packer/zip_with_dockerfile/README.md)

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

- **export `<service> <dir> [--raw]`**  
  Exports a service into the specified directory. Two modes:
  - **`nodo export <service> <dir>`** (default) → writes `<service>.celaut.bee`, a beerpc-framed package. This is the **importable / transmittable** artifact — share it and feed it to `nodo import`.
  - **`nodo export <service> <dir> --raw`** → writes a raw `<service>.celaut`. This is for **manual hash verification only** and is **NOT importable** — running `nodo import` on it fails with `Invalid file format: Incomplete message data`.  
  **Example:**  
  `nodo export MyService /export/dir`  
  `nodo export MyService /export/dir --raw`  *(verify-only, not importable)*

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
  Removes unused service instances (requires superuser privileges).  
  **Example:**  
  `sudo nodo prune_containers`

---

## No local Docker

nodo does **not** install or run Docker on the host. Services run as
**Cloud Hypervisor** microVMs, and packing is delegated to an external
**packer-service** (which runs Docker/buildx inside its own sealed microVM).
There is no `nodo docker` command and no isolated Docker daemon to manage.

To pack, point nodo at a packer service and run `nodo pack` (see the **pack**
command above):

```bash
# set the packer id under core_services in config.yaml (single source of truth):
#   core_services: [{ name: "packer", id: "<packer-service id>" }]
# download source (optional): packer.PACKER_SOURCE_URL: "<manifest url>"
#   when empty, nodo resolves the packer via the source-application core service.
nodo execute <packer-service id>               # start a running instance nodo resolves by id
# override only: packer.PACKER_SERVICE_URL: http://<ip>:8080  in config.yaml
nodo pack /path/to/project
```

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
