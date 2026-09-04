## Nodo: User Guide

This guide will help you understand and use the available commands in **Nodo**, a service orchestration tool for distributed networks. Below is a complete list of commands along with usage examples.

> New to Nodo? Start with the [End-to-End Walkthrough](WALKTHROUGH.md) (pack →
> estimate → execute → call → observe → kill, with example output), then use this
> page as the per-command reference. Concepts are defined in
> [`CONCEPTS.md`](CONCEPTS.md); configuration in [`CONFIG.md`](CONFIG.md);
> packing in [`PACKING.md`](PACKING.md); problems in
> [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

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

- **execute `[--remote] [--name <instance-name>] [-e key value] <service id | service tag | '.celaut.bee' file path>`**  
  Launches a service instance. Use `--remote` to advertise the host-facing IP instead of the internal VM/container IP. Use `--name` to assign a human-readable instance name. Use `-e` to add service enviroment variables.  
  **Example:**  
  `nodo execute 1234567890abcdef`
  `nodo execute --remote 1234567890abcdef`
  `nodo execute --remote -e workers 8 -e timeout 20 1234567890abcdef`

- **estimate `<service id | service tag | '.celaut.bee' file path>`**  
  Estimates service execution cost without launching it.  
  Prints:
  - execution feasibility (`YES/NO`)
  - reason when execution is not possible
  - estimated costs (to start, and maintenance per hour)
  
  **Examples:**  
  `nodo estimate 1234567890abcdef`  
  `nodo estimate my_service_tag`  
  `nodo estimate ./my-service.celaut.bee`

- **remove `<service id | service tag>`** (requires root)  
  Removes a service from the node: its registry entry, its metadata entry, and its
  built image (the guest rootfs cached under `CACHE/microvm/<id>/<arch>`,
  which is normally the bulk of the disk a service holds). Reports the bytes freed,
  or that no image was cached. Running instances of the service are not stopped --
  each already holds its own copy of the image -- and are counted in the output if any
  exist; the next `nodo execute` of that service rebuilds it.  
  **Example:**  
  `sudo nodo remove 1234567890abcdef`

- **prune `[--all] [--dry-run]`** (requires root, except `--dry-run`)  
  Reclaims the cache disk that no other command owns. `nodo remove` frees the bundle
  of a service you name; two directories under `CACHE/microvm/` grow with no
  owner at all:

  - `runtime/<vmachine_id>/` — an instance's own copy of its rootfs image. Normally
    freed by `kill`, so what is left is what a teardown did not finish freeing: a VM
    that died before `kill` ran, or a cleanup that errored partway through. Entries
    whose state file is gone are invisible to the janitor (which iterates state
    files) and are found here by walking the directory itself.
  - `failures/<vmachine_id>/` — the runtime directory of a failed launch, preserved
    for debugging by `virtualizers.ch.CONSERVE_RUNTIME_DIR_ON_FAILURE` and pruned by
    nothing. Entries older than `virtualizers.ch.FAILURE_RETENTION_DAYS` (7 by
    default) are reclaimable; `--all` takes them regardless of age.

  Orphaned VMs are torn down through `kill`, not deleted outright, so the tap device,
  cgroup, API socket and firewall rules go with the disk. Every entry is printed with
  its size and its reason — including the ones that were **kept** and why — and the
  reported total is what was actually freed, never what was attempted. `--dry-run`
  lists what would be removed without touching anything, and needs no root.  
  **Examples:**  
  `nodo prune --dry-run`  
  `sudo nodo prune`  
  `sudo nodo prune --all`

- **kill `<instance id>`** (requires root)  
  Stops a running service instance by ID.  
  **Example:**  
  `sudo nodo kill abcdef1234567890`

- **observe `<instance id> [--save <path>]`**  
  Attaches to a running instance and continuously displays live resource
  metrics (CPU and memory, current + session peak) **together with a live
  per-flow view of the microVM's network activity in the same frame**. Address
  the instance by its full instance id or its instance name. Press `Ctrl-C` to exit.  

  **Live network panel.** The network section is a live table of active flows,
  newest activity first. Each flow (direction + transport + addresses/ports) is
  one row that **accumulates** as packets arrive — packet count, byte total and
  a last-seen timestamp all tick up in place, so a chatty connection stays
  visibly alive next to the CPU/memory numbers instead of printing once and
  looking frozen. A row looks like:

  ```
  17:15:41  OUT → instance c92ae2ff [gateway] (parent)     TCP     142 pkts    38.4 KB
  ```

  The panel re-renders on network bursts (throttled to avoid flicker) as well as
  on the ~1 s metrics tick; CPU/memory and the flow table are always drawn in the
  same frame. This on-screen table is an **aggregation for readability** — the
  `.pcap` still records **every** frame verbatim, and `metrics.jsonl` remains
  metrics-only.

  **Network capture.** On the Linux/KVM host with `CAP_NET_RAW` (run as root),
  observe binds an `AF_PACKET` raw socket to the instance's *tap* interface and
  captures **every** frame in both directions — the Wireshark equivalent of the
  VM's whole NIC. Transport protocol (TCP/UDP/ICMP), ports, TCP flags and
  direction are read straight from the real IP/TCP/UDP headers; there is no
  port→app-name guessing. Packet timestamps are taken from the **kernel**
  (`SO_TIMESTAMPNS`), so the pcap has accurate inter-packet timing. The pcap
  link-type is **auto-detected** from the interface: a normal L2 tap
  (`ARPHRD_ETHER`) records as `LINKTYPE_ETHERNET`, a raw-IP tun device
  (`ARPHRD_NONE`) as `LINKTYPE_RAW`. If `AF_PACKET` is unavailable (non-root,
  non-Linux, or the tap can't be found) it degrades to the legacy `conntrack`
  table scan for the on-screen feed (byte counts show `conntrack`), labels the
  degraded mode, and writes no `.pcap`.  

  **Saving (`--save <path>`).** By default nothing is stored. When `--save` is
  passed, `<path>` is treated as a **directory**: observe creates
  `<path>/<tag>_<instance_id>/` (or `<path>/<instance_id>/` when the service
  has no tag) and writes, live while the display runs:
  - `metrics.jsonl` — one JSON object per second with the CPU + memory sample
    shown in the live panel (`cpu_percent`, `cpu_peak_percent`, `mem_bytes`,
    `mem_peak_bytes`).
  - `capture.pcap` — **every** captured frame in standard libpcap format
    (auto-detected link-type, 65535 snaplen), openable directly in Wireshark /
    `tcpdump -r`. Written only when real packet capture is active.
  - `capture_unavailable.txt` — written **instead** of the pcap when capture
    degraded to conntrack, stating why no pcap was produced (e.g. missing
    `CAP_NET_RAW` / non-Linux host), so the artifact folder is self-explanatory.

  **Examples:**  
  `nodo observe 8a7fd2c1e094b6f0`  
  `nodo observe my-instance --save ./captures`  
  → `./captures/gateway_8a7fd2c1e094b6f0/{metrics.jsonl,capture.pcap}`, then
  `wireshark ./captures/gateway_8a7fd2c1e094b6f0/capture.pcap`

  **Observe over the gateway (`Gateway.Observe` bee_rpc):** the same live data is
  exposed as a streaming RPC so peers/agents can subscribe remotely instead of
  attaching a terminal. It shares the exact capture core the `observe` command
  uses (`observe_event_stream`) — no duplicated logic.
  - **Input:** one `ObserveRequest { instance_id, include_packets }`. The
    `instance_id` addresses the instance the same way `GetMetrics` does
    (`TokenMessage.token` semantics — full instance id or its instance name). Set
    `include_packets = true` to also receive raw per-packet records; leave it
    `false` (default) for the lighter metrics-only stream.
  - **Output:** a live stream of `ObserveEvent`. Each event names its payload via
    `kind`:
    - `session` — sent first: `capture_mode` (`pcap` | `conntrack`) and
      `degraded_reason` so the client knows whether full AF_PACKET capture is
      active.
    - `metrics` — one CPU + memory snapshot per second, mirroring the
      `metrics.jsonl` fields (`cpu_percent`, `cpu_peak_percent`, `mem_bytes`,
      `mem_peak_bytes`).
    - `packet` — one parsed connection event (direction, transport, ports, TCP
      flags, classified peer). Emitted per frame in `pcap` mode, per conntrack
      row in the fallback.
    - `notice` — degraded-mode / lifecycle messages (e.g. *instance stopped*),
      never fabricated data.
  - The stream ends cleanly when the instance stops or the client cancels (the
    AF_PACKET socket is released on cancellation). Instance-not-found /
    not-running is reported as a trailing degraded `notice`.
  - Full AF_PACKET capture needs a Linux host with `CAP_NET_RAW`; elsewhere the
    RPC degrades to the conntrack fallback exactly like the CLI.

- **tunnel `<instance id> <slot> [--udp] [--listen <port>] [--host <addr>] [--peer <host:port>] [--idle <seconds>]`**  
  Binds a local port and forwards its traffic to `<slot>` of the instance through
  the node's `Gateway.ServiceTunnel` stream, so a service can be reached without
  publishing a port of its own. `<slot>` must be a port the service **declares**
  in its API (see `nodo instances` / the instance's `uri_slot`); undeclared ports
  are refused. With `--peer` the tunnel goes through a remote node and
  `<instance id>` must be the token as **that** node knows it, since only it can
  resolve the token. The listener binds to `127.0.0.1` unless `--host` says
  otherwise, and `--listen` is optional (an ephemeral port is picked and
  printed). Press `Ctrl-C` to stop.  

  `--udp` makes the local socket a datagram socket, for slots that declare UDP;
  the node picks the node-to-service transport from the slot's own declaration,
  so the two must match. TCP gives each connection its own stream. UDP has no
  connections, so traffic is keyed by source address and a flow is dropped after
  `--idle` seconds of silence (30 by default). Datagram boundaries are preserved,
  but a tunnelled datagram is reliable and ordered rather than lossy — see
  [TUNNELING.md](TUNNELING.md) for that and the rest of the wire protocol.  
  **Examples:**  
  `nodo tunnel my-instance 8080` → then `curl http://127.0.0.1:<printed port>/`  
  `nodo tunnel abcdef1234567890 8080 --listen 9000`  
  `nodo tunnel abcdef1234567890 5353 --udp --listen 5353`  
  `nodo tunnel abcdef1234567890 8080 --peer 192.168.1.10:4040`  

  The node also opens these tunnels for itself: when a service is delegated to a
  peer whose advertised addresses this node cannot reach, it stands in for the
  service locally and hands our client an endpoint of its own. That is controlled
  by `network.DELEGATION_TUNNEL_POLICY` (`auto` / `always` / `never`).

- **increase_deposit `<instance id> <amount>`**  
  Adds to a service instance's deposit. The amount is in `ui.DISPLAY_UNIT` (ERG by default).  
  **Example:**  
  `nodo increase_deposit abcdef1234567890 0.01`

- **decrease_deposit `<instance id> <amount>`**  
  Takes back part of a service instance's deposit.  
  **Example:**  
  `nodo decrease_deposit abcdef1234567890 0.005`

- **services**  
  Lists all available services on the node.  
  **Example:**  
  `nodo services`

- **connect `<ip:port>`**  
  Manually connects to a peer node. The address is dialled and the identity that answers
  is verified, so it is registered under that peer and **taken away from any other peer
  still holding it** — an address reaches one node, and the usual reason two peers claim
  it is that the host reinstalled and came back under a new identity key (its peer_id).
  The old peer keeps whatever other addresses it announced; forget it with
  `nodo disconnect` (or `d` on the TUI's Peers page) if it has none left.  
  **Example:**  
  `nodo connect 192.168.1.10:4040`

- **pack `<project directory>`**  
  Packages a project into a service. There are two backends, selected by
  `packer.local` in `config.yaml`:

  **Default (`packer.local: false`) — packer-service:** nodo does **not** build
  locally. It sends the project to an external **packer-service** (a microVM that
  runs Docker/buildx in a sealed VM, so Docker is never installed on your host)
  and imports the returned `.celaut.bee`. Configure the packer by its published
  service id first, then `nodo execute` it so a running instance exists:  
  set the packer id under `core_services` in `config.yaml` — the single source of
  truth: `core_services: { packer: "<packer-service id>" }`  
  nodo resolves the running instance's `ip:port` automatically. When nodo needs to
  download the packer it uses `packer.PACKER_SOURCE_URL` if set, otherwise the
  source-application core service. To override with an out-of-band packer instead,
  set `packer.PACKER_SERVICE_URL: http://<ip>:8080` in `config.yaml`  

  **Optional (`packer.local: true`) — local rootless packer:** nodo builds the
  service on this host with its **own rootless BuildKit toolchain**. Nothing is
  installed at node-install time — the first local pack provisions it on demand
  via `bash/install_buildkit.sh` (node-local, independent of any Docker already on
  the host, mirroring `install_java.sh`). nodo starts the builder right before the
  build and stops it right after, and only one `nodo pack` may run at a time.
  The builder runs as your own user, so packing never asks for sudo. Tune it with
  `packer.buildkit.*` and `dependencies.buildkit.*` in `config.yaml`.  

  **Example:**  
  `nodo pack /path/to/project`
  > **Before packing, read [`PACKING.md`](PACKING.md)** — it is the canonical
  > reference for the project layout, `pack_config.json`, `service.json`, and the
  > `Dockerfile` rules (notably: no `CMD` / `ENTRYPOINT` / `EXPOSE`; the entrypoint
  > is declared in `service.json → init.entry_path`). Do not guess the format.

- **tui**  
  Launches the terminal user interface for monitoring and managing the node. Its
  Config page is also where environment and runtime settings are edite ([`CONFIG.md`](CONFIG.md)).  
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
  Downloads a published service from a manifest URL and imports it locally (the service id is recomputed from content on import).
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

- **credit_client `<client id> <amount>`**  
  Adds to a client's balance. The amount is in `ui.DISPLAY_UNIT` (ERG by default).  
  **Example:**  
  `nodo credit_client abcdef1234567890 0.01`

- **debit_client `<client id> <amount>`**  
  Takes back part of a client's balance.  
  **Example:**  
  `nodo debit_client abcdef1234567890 0.005`

---

## Estimate Resource Calculation Notes (internal)

These are the **internal** server-side calculations `nodo estimate` performs to
decide feasibility; they are **not** printed by the command (its output is the
feasibility verdict and the price figures only). `nodo estimate` uses the same
internal checks as runtime cost estimation:

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
  - **Inbound reachability**: gateway port resolvable and listening, and DDNS resolution — deferring the router steps to `nodo nat-guide`  
  **Example:**  
  `sudo nodo doctor`

- **nat-guide**  
  Prints how to make this node reachable from the Internet: which port to forward on
  your router (with this machine's own address, port and detected router filled in),
  how DDNS fits, how to test it from outside, and what to check when it still fails
  (CGNAT, a second router, the host firewall). Does **not** require superuser.  

  Only the **gateway port** needs forwarding: service tunneling carries every service
  through it, so `FREE_PORTS_RANGE` only matters if you also want direct exposure.
  Nothing here verifies the forwarding from outside — that cannot be done from this
  host, since a connection from inside your own network succeeds either way.  
  **Example:**  
  `nodo nat-guide`

- **migrate**  
  Updates the database schema.  
  **Example:**  
  `nodo migrate`

- **force_execution `<peer_id>` `<service id|tag|'.celaut' path>` `[-e key value]` `[--name instance-name]`**  
  Testing/dev only. `execute` always picks the peer through `execution_balancer`
  (cheapest local-or-connected-peer candidate, tried in cost order). This command
  skips that entirely and delegates straight to `peer_id` — no comparison against
  `local` or any other peer, and no fallback if it fails. It still goes through the
  normal cost accounting for the delegated instance (the peer's own cost
  estimate, `spend_mu`, `balance_on_other_peer`) — only peer *selection* is
  skipped. Fails immediately if `peer_id` isn't currently connected (see `nodo peers`).
  Useful for exercising peer-to-peer delegation and tunneling deterministically
  without disconnecting every other peer first.  
  **Example:**  
  `nodo force_execution a1b2c3d4-... my-service`

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

- **sync_reputation_proof**  
  Reconciles the locally configured reputation proof with the wallet mnemonic and reports
  every step. It (1) validates the currently configured proof, (2) removes it from the
  config if it is not owned by the configured wallet, (3) if a mnemonic is configured,
  looks up an on-chain reputation proof owned by that wallet and stores its id in the
  config when one exists. Run it by hand after changing the mnemonic, so the new wallet
  picks up its associated reputation proof (if any).  
  **Example:**  
  `nodo sync_reputation_proof`

- **refresh_ergo_nodes**  
  Refreshes the Ergo nodes list and selects one as a provider.  
  **Example:**  
  `nodo refresh_ergo_nodes`

- **prune_containers**  
  Removes unused service instances (requires superuser privileges).  
  **Example:**  
  `sudo nodo prune_containers`

---

## Docker backends

nodo supports two packing backends, and **no Docker is installed or run on the
host by default**. Services run as **Cloud Hypervisor** microVMs, and packing is
normally delegated to an external **packer-service** (which runs Docker/buildx
inside its own sealed microVM). In this default mode there is no Docker daemon
for nodo to manage.

With **`packer.local: true`**, nodo instead builds services on this host with its
**own rootless BuildKit toolchain** (see the **pack** command above). It is
provisioned on demand under `MAIN_DIR` via `bash/install_buildkit.sh`, kept
independent of any Docker already on the host; nodo starts the builder right
before a build and stops it right after. `docker buildx` is not involved: buildx
is only a front end for BuildKit, and driving BuildKit directly is what lets the
builder run unprivileged. That builder is queried with
`nodo local_builder <buildctl args>` (no root needed), listed in `nodo help`.

To pack with the default external backend, point nodo at a packer service and run
`nodo pack` (see the **pack** command above):

```bash
# set the packer id under core_services in config.yaml (single source of truth):
#   core_services: { packer: "<packer-service id>" }
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

Run `nodo tui` to open the operations console. Its pages cover node/host statistics, current
instance resource usage and reservations, local services, peers, clients, complete
`config.yaml` editing, logs, storage, and Ergo wallet balances. The old tunnels page was
removed because nodo does not use it.

- `Tab`/`Shift+Tab` switches pages; Up/Down selects rows.
- `r` refreshes.
- On Peers, `c` connects a peer, `d` forgets the selected one (`nodo disconnect`), and `+`/`-` adjust its
  reputation. The detail card shows what this node has paid that peer and the events behind its score.
- On Clients, `+`/`-` credit/debit the selected client's balance (`nodo credit_client`/`debit_client`),
  and the detail card shows what a client has paid, its deposit tokens, and the instances it
  started here.
- On Services, `e` executes the selected service and `d` deletes it.
- On Config, Right/Left enter and leave a branch of the tree, `e` edits any selected YAML
  value, `/` filters values, and `x` clears the filter. Secrets are masked, comments are
  preserved, and each write snapshots the previous file to `config-<timestamp>.yaml`.
- On Cell, the node's policies are laid out as a cell: Right/Left move between organelles,
  Up/Down between the decisions inside one, and Enter moves a decision to its next position
  (after showing every key it would change). `p` applies a whole posture — "just me",
  "cautious renter", "open renter", "lan lab", "workbench" — and `d` shows exactly where
  this node differs from the one it is closest to. `n` prints the router steps.
- **Every configuration change from the TUI backs up `config.yaml`, writes it, and restarts
  nodo — and puts the backup straight back if the node does not come up on it.** So the
  file always describes the node that is running, and no change is left waiting for a
  restart somebody has to remember. The restart drives `systemctl`, so editing
  configuration on a serving node needs root.
- `q`, Escape, or Ctrl+C exits.

See [the TUI reference](../src/commands/tui/README.md) for page details, refresh behavior, and
the typed configuration editor contract.

---

## Shell Completion

Nodo ships `<Tab>` completion for **bash** and **zsh**. It completes command names and, for the
commands that take one, the identifier of the relevant object:

- **Service id or tag** — `execute`, `estimate`, `inspect`, `remove`, `publish`, `tag`,
  `export`, `integrity`
- **No argument** — `prune` (flags only: `--all`, `--dry-run`)
- **Instance id or name** — `kill`, `observe`, `tunnel`, `increase_deposit`, `decrease_deposit`
- **Peer id** — `disconnect`, `increase_peer_deposit`
- **Client id** — `credit_client`, `debit_client`
- **Subcommands** — `daemon start|status|stop|restart`

The installer sets this up automatically. To (re)install it yourself:

```bash
nodo completion install          # per-user, or system-wide when run as root
nodo completion install --user   # force the per-user location
nodo completion install --system # force the system-wide location
```

You can also print a script to place wherever you like:

```bash
nodo completion bash > /etc/bash_completion.d/nodo
nodo completion zsh  > /usr/local/share/zsh/site-functions/_nodo
```

Open a new shell to pick it up. Bash needs the `bash-completion` package installed; zsh needs the
install directory on your `fpath`. Completion never edits your shell rc files. The dynamic
candidate list comes from `nodo completion list <commands|services|instances|peers|refs>`, which is
deliberately lightweight so it stays fast on every keypress.

---

## Uninstalling

To remove Nodo — automatically (`uninstall.sh`) or manually — follow the
[Uninstallation Guide](UNINSTALL.md). The installer touches a systemd unit, a
wrapper at `/usr/local/bin/nodo`, the `TARGET_DIR` install root, and system-level
shell completions (`/etc/bash_completion.d/nodo` and
`/usr/local/share/zsh/site-functions/_nodo`); the guide covers each — note that
`uninstall.sh` does not remove the completions.

---

## Getting Help

To view a summary of all available commands, simply run:

```bash
nodo
```
