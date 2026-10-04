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

The second first-run question, the share of earnings this node donates, is never asked without a terminal: with no TTY it is simply left for the next interactive run. To answer it headlessly, set `NODO_DONATION_PERCENTAGE` (a share between `0` and `1`, e.g. `0` to donate nothing) in the environment of the first command.

Every command an agent needs is non-interactive and most have a `--json` form; see [Scripting and AI agents](#scripting-and-ai-agents-) for the conventions, the JSON shapes, and the TUI ↔ CLI mapping.

> ⚠️ Creating this file means you accept the Know Your Assumptions ([`docs/KyA.md`](KyA.md)) without reading the interactive prompt. Only do this in environments you control.

---

## Basic Commands

These are the most commonly used commands for daily tasks:

- **execute `[--name <instance-name>] [-e key value] <service id | service tag | '.celaut.bee' file path>`**  
  Launches a service instance. The address it prints is reachable from this host only; use `nodo tunnel` to reach the instance from elsewhere. Use `--name` to assign a human-readable instance name. Use `-e` to add service enviroment variables.  
  **Example:**  
  `nodo execute 1234567890abcdef`
  `nodo execute -e workers 8 -e timeout 20 1234567890abcdef`

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

- **kill `<instance id> [--json]`** (requires root)  
  Stops a running service instance by ID or name, and closes the tunnels this host
  opened to it (`nodo tunnels --instance <id>`): a tunnel to a stopped instance would
  keep its port bound and fail every connection. Each is closed the way
  `nodo tunnel_close` closes it, so none of them is reopened after a restart. Exits `1`
  when the instance could not be stopped.
  JSON: `{"killed": "<id>", "tunnels": {"closed": ["3f9a0c12"], "failed": []}}`.  
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

- **tunnel `<instance id> <slot> [--udp] [--listen <port>] [--host <addr>] [--peer <host:port>] [--idle <seconds>] [--detach] [--json]`**  
  Binds a local port and forwards its traffic to `<slot>` of the instance through
  the node's `Gateway.ServiceTunnel` stream, so a service can be reached without
  publishing a port of its own. `<slot>` must be a port the service **declares**
  in its API (see `nodo instances` / the instance's `uri_slot`); undeclared ports
  are refused. With `--peer` the tunnel goes through a remote node and
  `<instance id>` must be the token as **that** node knows it, since only it can
  resolve the token. The listener binds to `127.0.0.1` unless `--host` says
  otherwise, and `--listen` is optional (an ephemeral port is picked and
  printed). Press `Ctrl-C` to stop, or close it from anywhere with
  `nodo tunnel_close <id>`. Exits `1` when the local port cannot be bound.  

  Every tunnel registers itself while it runs (`<main.STORAGE>/tunnels/<id>.json`,
  removed on exit), which is what `nodo tunnels` and the TUI's TUNNELS page list.
  `--detach` starts it in the background and returns once the listener is bound,
  printing its id; its output goes to `<main.STORAGE>/tunnels/<id>.log`. This is
  the form for scripts, agents and the TUI, none of which can keep a terminal open
  for the tunnel's lifetime. A detached tunnel survives a reboot or a daemon
  restart: its spec (`<id>.spec`: instance, slot, flags, pinned to the port it got)
  is kept, and the daemon reopens it on start under the same id and port, the way
  delegated endpoints are restored. Closing it on purpose (`nodo tunnel_close`, `d`
  in the TUI, `nodo kill` of its instance) drops the spec; one whose instance no
  longer exists is dropped with a log line, and one that fails to start three
  daemon starts in a row is dropped too. A foreground tunnel is not reopened.
  `--json` prints the tunnel as one object — at once
  with `--detach`, or as the first line in the foreground (the per-connection log
  then goes to stderr):
  ```json
  {"tunnel": {"id": "3f9a0c12", "pid": 41872, "instance": "my-instance",
   "token": "abcdef1234567890", "slot": 8080, "transport": "tcp",
   "listen_host": "127.0.0.1", "listen_port": 40517, "gateway": "127.0.0.1:8090",
   "peer": null, "detached": true, "log": "/nodo/storage/tunnels/3f9a0c12.log",
   "started_at": 1790000000, "open_fee_mu": 10000, "age_secs": 0,
   "persistent": true},
   "read_at": 1790000000}
  ```
  A tunnel that could not start is `{"error": "Error: cannot bind …"}` with exit `1`.  
  **Fee.** Opening the listener is free; each connection through it (each UDP flow)
  opens a `ServiceTunnel` stream, which spends `pricing.TUNNEL_OPEN_MU` of the
  instance's balance on the relaying node, plus traffic (`pricing.NET_MU_PER_GIB`).
  The output says how much (`Fee: …`; `open_fee_mu` in JSON, `null` through `--peer`,
  whose node charges its own price). There is no prompt — scripts and agents run
  this — while the TUI asks y/N with the amount before opening one.  

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
  `nodo tunnel my-instance 8080 --listen 9000 --detach`  

  The node also opens these tunnels for itself: when a service is delegated to a
  peer whose advertised addresses this node cannot reach, it stands in for the
  service locally and hands our client an endpoint of its own. That is controlled
  by `network.DELEGATION_TUNNEL_POLICY` (`auto` / `always` / `never`). Those are
  part of the delegated instance, not `nodo tunnel` processes, and are not listed by
  `nodo tunnels`.

- **tunnels `[<tunnel id> | --instance <instance> | --inbound] [--json]`**  
  Lists the tunnels running on this host — every `nodo tunnel`, detached or in a
  terminal — with where each listens, the slot it reaches, the instance, through
  which node, its pid and age. A tunnel id (or an unambiguous prefix of one) shows
  that tunnel with the last lines of its log. Files left by a tunnel that died
  without cleaning up (killed with `-9`, a reboot) are swept, not listed. These are
  the client ends this host opened.  
  `--inbound` is the other end: the `ServiceTunnel` streams this node is relaying
  for others right now — who (the caller's address), which instance and slot, the
  protocol, bytes in (caller → service) and out so far, and age. The daemon keeps
  them in memory and mirrors them to `<main.STORAGE>/tunnels/inbound.snapshot` (every
  open and close, and every 2 s while one is open); a snapshot left by a daemon that
  is no longer running lists nothing. List only: they cannot be closed from here.
  `--instance <instance>` lists only the tunnels that reach that instance (by id or
  name, through this node) — the instance → tunnels table the TUI shows under the
  INSTANCES card. `nodo instances --json` also carries each local instance's tunnel
  ids in `tunnels`.
  JSON: `{"tunnels": [tunnel, …]}` / `{"tunnel": {…, "log_tail": ["…"]}}` /
  `{"instance": "web", "tunnels": [tunnel, …]}`, with `tunnel` as above. An unknown
  or ambiguous id is `{"error": …}`, exit `1`. `--inbound`:
  `{"inbound": [{"id", "caller", "token", "slot", "transport", "target",
  "started_at", "bytes_in", "bytes_out", "age_secs"}, …], "snapshot_at": 1790000000}`
  (`snapshot_at` is `null` when the node is not running).  
  **Examples:**  
  `nodo tunnels`  
  `nodo tunnels 3f9a --json`  
  `nodo tunnels --instance my-instance --json`  
  `nodo tunnels --inbound`

- **tunnel_close `<tunnel id>... | --all` `[--json]`**  
  Stops tunnels: SIGTERM, and SIGKILL if one is still running five seconds later.
  The tunnel closes its listener and removes its own registry file, and a detached
  one is no longer reopened when the node restarts. A tunnel
  started by another user (root, typically) needs that user. Exits `1` if any
  named tunnel was not found or could not be stopped.
  JSON: `{"closed": ["3f9a0c12"], "failed": []}`.  
  **Examples:**  
  `nodo tunnel_close 3f9a0c12`  
  `nodo tunnel_close --all`

- **increase_deposit `<instance id> <amount>`**  
  Adds to a service instance's deposit. The amount is in `ui.DISPLAY_UNIT` (ERG by default).  
  **Example:**  
  `nodo increase_deposit abcdef1234567890 0.01`

- **decrease_deposit `<instance id> <amount>`**  
  Takes back part of a service instance's deposit.  
  **Example:**  
  `nodo decrease_deposit abcdef1234567890 0.005`

- **services `[<service id | tag>] [--json] [--limit N]`**  
  Lists all available services on the node. With a service, shows its reputation
  score on this node and the events behind it. `--json` for one JSON object
  ([shape](#new-commands-for-tui-parity)).  
  **Example:**  
  `nodo services`  
  `nodo services my_service_tag --json`

- **connect `<ip:port>`**  
  Manually connects to a peer node. The address is dialled and the identity that answers
  is verified, so it is registered under that peer and **taken away from any other peer
  still holding it** — an address reaches one node, and the usual reason two peers claim
  it is that the host reinstalled and came back under a new identity key (its peer_id).
  The old peer keeps whatever other addresses it announced; forget it with
  `nodo disconnect` (or `d` on the TUI's Peers page) if it has none left.  
  **Example:**  
  `nodo connect 192.168.1.10:4040`

- **pack `<project directory | https git URL[#subdir]> [--local] [--detach] [--json]`**  
  Packages a project into a service. The source is a local directory (relative
  paths are read from the shell you typed in) or an **https** git URL, optionally
  with `#<subdir>` for a project inside the repository. `http://` is refused (the
  code would be sealed as it arrived, and over plain http it can be changed on the
  way), and so are `ssh://` / `git@…` URLs (the repository is cloned without
  credentials: clone it yourself and pack the folder). Exits `1` when the source is
  refused or the pack produces no service. There are two backends, selected by
  `packer.local` in `config.yaml`, or by `--local` for one run:

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

  **`--local` — local packer for this run only:** use the local rootless packer
  for this pack, and also for the dependencies that it packs. The `config.yaml`
  file does not change.

  **If the packer service is not available** (`packer.local: false`):
  - In a terminal, nodo asks you to enable the local packer. If you answer yes,
    nodo writes `packer.local: true` to `config.yaml` and continues the pack.
  - Without a terminal (a script, CI or an AI agent), nodo does not ask. The pack
    fails and nodo shows a hint.

  **For AI agents and scripts:** if `nodo pack` fails because the packer service
  is not available, run the same command again with `--local`. The first local
  pack can install BuildKit with `bash/install_buildkit.sh`. This install can ask
  for sudo one time. Do not use `--local` if the operator wants packs to stay off
  this host.

  **`--fast` / `--optimize` — single-block packing (local packer only):**
  `--fast` inlines the whole image into one filesystem block instead of storing
  each file at or over `packer.MIN_BUFFER_BLOCK_SIZE` as its own content-addressed
  block — quicker to pack, but the pack reserves memory for the whole image
  (`packer.PACKER_MEMORY_SIZE_FACTOR` × its size), the node that builds the VM
  reads the whole image into memory, and nothing is deduplicated with other
  services. `--optimize` forces the normal per-file blocks for one run;
  `packer.fast: true` in `config.yaml` makes fast the node's default. Both modes
  produce the same service id. The packer service ignores them (with a warning).

  A fast-packed service costs its whole image in memory at **every build**, not
  only at pack time, until it is re-packed with `--optimize` — which replaces the
  stored single block with the per-file one (same id). A fast pack falls back to
  per-file blocks by itself, and says so, when the image is over
  `packer.FAST_MAX_BYTES` (1 GiB) or the node lacks the RAM fast mode reserves.

  Every pack records itself in `<main.STORAGE>/packs/<id>.json` while it runs and
  keeps the record afterwards with its outcome, so `nodo packs` and the TUI's PACKS
  page see packs started anywhere — a terminal, a script, the TUI. **`--detach`**
  runs the pack in the background and returns at once with its id; its output goes
  to `<main.STORAGE>/packs/<id>.log`. That is the form for scripts, agents and the
  TUI, none of which can hold a terminal open for a build that takes minutes. A
  detached pack with the local packer that finds another pack using the builder
  waits for it (`queued`) instead of failing, as one in a terminal does.
  `--json` prints one object: with `--detach`, the pack as it started
  ```json
  {"pack": {"id": "3f9a0c12", "pid": 41872, "source": "/home/me/hello", "kind": "dir",
   "packer": "local", "status": "running", "stage": "starting", "detached": true,
   "log": "/nodo/storage/packs/3f9a0c12.log", "started_at": 1791051095,
   "finished_at": null, "service_id": null, "error": null}}
  ```
  and in the foreground, the same record once it finished (the packer's own output
  goes to stderr). A source that is refused is `{"error": "Error: …"}` with exit `1`.  
  **Examples:**  
  `nodo pack /path/to/project`  
  `nodo pack https://github.com/celaut-basics/demo-service.git#hello --detach`  
  `nodo pack /path/to/project --local`  
  `nodo pack /path/to/project --local --fast`  
  `nodo pack ./my-service --detach --json`
  > **Before packing, read [`PACKING.md`](PACKING.md)** — it is the canonical
  > reference for the project layout, `pack_config.json`, `service.json`, and the
  > `Dockerfile` rules (notably: no `CMD` / `ENTRYPOINT` / `EXPOSE`; the entrypoint
  > is declared in `service.json → init.entry_path`). Do not guess the format.

- **packs `[<pack id>] [--active] [--json]`**  
  Lists the packs on record, newest first: id, status, source, the service id it
  produced (or the stage a running one is at, or why one failed), age and how long
  it took. `--active` keeps only the `queued`/`running` ones. A pack id (or an
  unambiguous prefix) shows that pack with the last lines of its log. Statuses:
  `queued` (waiting for another local pack to release the builder), `running`,
  `done`, `failed`, `cancelled`. A record that says running but whose process is
  gone (killed with -9, or the host restarted) is reported — and rewritten — as
  `failed` with `error` saying so. The newest 20 finished packs are kept; older
  records and their logs are pruned.
  JSON: `{"packs": [pack, …]}` / `{"pack": {…, "log_tail": ["…"]}}`, with `pack` as
  above plus `age_secs`, `duration_secs` and `last_line` (the last line of its log).
  `stage` is one of `starting`, `waiting for another pack`, `starting the builder`,
  `starting the packer service`, `cloning`, `copying the project`,
  `waiting for the packer service`, `uploading dependencies`, `zipping the project`,
  `building`, `building in the packer service`, `importing`; `null` once finished.  
  **Examples:**  
  `nodo packs`  
  `nodo packs 3f9a --json`

- **pack_cancel `<pack id>… [--json]`**  
  Stops a queued or running pack. It is sent SIGTERM and unwinds through the
  packer's own cleanup — stops nodo's rootless builder, removes the clone or copy,
  releases the pack lock — and records itself `cancelled`. One still alive after
  30 s is killed, with its whole process group when it was started with `--detach`
  (the `git` / `buildctl` it was waiting on live there). What it cannot stop: a
  build already sent to a packer **service** keeps running inside that VM until it
  finishes; its result is just never imported. Exits `1` if a pack is unknown, not
  running, or another user's.
  JSON: `{"cancelled": ["3f9a0c12"], "failed": []}` (+ `"error"` when `failed` is
  not empty).

- **tui**  
  Launches the terminal user interface for monitoring and managing the node. Its
  All page is **the** place to change a setting: it validates the value, backs the
  file up, writes it, and restarts the node in one step ([`CONFIG.md`](CONFIG.md)).  
  **Example:**  
  `nodo tui`

- **`nodo` (no arguments)**  
  Prints the quick-start command list, then this node's own status: service status,
  version, the node's identity key, address and balances, and — last, so it is the
  last thing read — any operator alerts that need action. The
  `Node id:` line is the node's Ed25519 public key in hex — the same string the
  reputation system keys every opinion by, so it is what you compare against when a
  peer says it vouched for you, and what you paste when asking one to. It reads
  `unavailable (no identity mnemonic yet)` on a node that has not been started long
  enough to derive one. When there are alerts, it also tells you to run
  `sudo nodo daemon restart`.  
  **Example:**  
  `nodo`

- **logs `[-n <lines>] [--json]`**  
  Shows real-time application logs for monitoring (follows until Ctrl+C). With
  `-n`, prints the last N lines and exits; `--json` prints them as one object.  
  **Example:**  
  `nodo logs`  
  `nodo logs -n 200`

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

- **download `<manifest url | .celaut.bee https url>`**  
  Downloads a published service and imports it locally (the service id is recomputed from content on import). Accepts either a manifest URL listing chunk URLs (one per line, `nodo publish`'s default output) or a direct HTTPS link to a `.celaut.bee` artifact, downloaded in a single request.
  **Examples:**  
  `nodo download https://raw.githubusercontent.com/user/repo/main/uploads/<service_hash>/manifest`  
  `nodo download https://raw.githubusercontent.com/user/repo/main/uploads/<service_hash>/manifest -o /tmp/services`  
  `nodo download https://example.com/path/to/service.celaut.bee`

- **get `<service id | service tag> [--now]`**  
  Asks the network for a service this node does not hold, by hash. By default the id
  is queued: the running node looks for it among its peers on its own, using
  `Gateway.GetService`, the next time its manager thread ticks. With `--now`, this
  command asks every known peer itself and blocks until it is found (or every peer
  has been tried).
  **Examples:**  
  `nodo get 1234567890abcdef`  
  `nodo get 1234567890abcdef --now`

- **integrity `[<service id | service tag>] [--fix]`**  
  Verifies registry/metadata integrity for all services or a specific one.
  Use `--fix` to repair detected inconsistencies.
  **Examples:**  
  `nodo integrity`  
  `nodo integrity my_service_tag --fix`

- **instances `[<search>] [--json]`**  
  Lists all running instances and their details. A search term filters them;
  `--json` adds raw values and live usage counters
  ([shape](#new-commands-for-tui-parity)).  
  **Example:**  
  `nodo instances`  
  `nodo instances web --json`

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

- **clients `[<client id>] [--json] [--limit N]`**  
  Lists clients currently connected to the node. With a client id, adds what it
  paid, its deposit tokens, the instances it started here and the peer it is bound to.  
  **Example:**  
  `nodo clients`  
  `nodo clients <client id> --json`

- **peers `[<peer id>] [--json] [--limit N]`**  
  Displays the list of connected peer nodes. With a peer id, adds every payment
  made to it and the reputation events behind its score.  
  **Example:**  
  `nodo peers`  
  `nodo peers <peer id> --json`

- **protocol `[<peer id> | <ip:port>] [--json] [--no-prose]`**  
  Without an argument, prints the protocol this node announces on every address:
  the signature scheme, the transport and the stack of layers (tls, http2, grpc,
  bee-rpc, celaut-gateway), each with its `formal` parameters and its prose. With a
  peer, asks it for its announcement and compares it with this node's, layer by layer,
  naming every `formal` key that differs. A layer is `compatible` when it differs
  only by message fields or RPCs that one side declares and the other does not:
  protobuf and gRPC let the two nodes talk. Exits 0 when the peer speaks this node's
  protocol on at least one address.  
  **Example:**  
  `nodo protocol`  
  `nodo protocol <peer id> --json`

- **peer_reputation `<peer id> <+N|-N>` `[--json]`**  
  Moves this node's local reputation score of a peer and records why
  (`operator_adjustment`) — the TUI's `+`/`-` on PEERS.  
  **Example:**  
  `nodo peer_reputation <peer id> -1`

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
  - **Inbound reachability**: gateway port resolvable and listening — deferring the router steps to `nodo nat-guide`  
  **Example:**  
  `sudo nodo doctor`

- **nat-guide**  
  Prints how to make this node reachable from the Internet: which port to forward on
  your router (with this machine's own address, port and detected router filled in),
  how to test it from outside, and what to check when it still fails (CGNAT, a
  second router, the host firewall). Does **not** require superuser.  

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

- **reputation `[<node id>]` `[--json]`**  
  What the network stakes on this node: every reputation proof out there that has put
  part of itself behind it, for or against. Read-only — explorer reads
  only, nothing is signed or spent. A figure is the *share* of what a staking proof
  has assigned to opinions — not of everything it minted, most of which sits in its own
  reserve box; see
  [Reputation system implementation](ERGO.md#reputation-system-implementation). Pass a
  node's identity public key (a peer id) to ask the same question about a peer, and
  `--json` for the machine-readable report the TUI's EARNINGS page reads. Beside each
  share it reports what that share cost: the ERG burned into the publishing proof, which
  the contract makes unrecoverable, apportioned by the share committed. Minting a proof
  is free, so the two figures have to be read together. The subject's vouch for itself is
  listed apart and left out of the standing — every node that has submitted holds one, and
  a node with no peers assigns its whole supply to it — but only the proofs it announced
  can be recognised as its own, so this separates the honest case and does not defend
  against a proof kept off its advertisement (issue #353).  
  **Example:**  
  `nodo reputation`

- **pay**  
  Pays a peer from this node's wallet, in **the asset's own unit** — ERG, BTC, whole
  SigUSD — whatever `ui.DISPLAY_UNIT` says, because what moves is an on-chain transfer
  and the asset denominates it.

  `--payment-method <ledger>:<asset>` names which one, and `--ledger <name>` plus
  `--asset <symbol|token id>` says the same thing the long way. It is only needed when
  this node offers more than one payment method, and then it is needed: two methods are
  two currencies, and guessing would move money nobody named. A *ledger* is not enough
  on its own, because one Ergo contract is paid in ERG and in every configured token, at
  different rates — the asset is matched by its symbol, its display-unit name or its
  64-hex id. A method that *is* named is where the payment settles or it does not
  happen: the amount was read in that method's unit and checked against its floors and
  wallet, and a peer that does not share it is refused, naming what it does offer.

  Where none is named — the automatic refill, `increase_peer_deposit` — funding is the
  selection: the node walks the methods it shares with the peer and uses the first whose
  wallet can cover it, so a node out of SigUSD but holding ERG tops up a peer that
  accepts both in ERG with no setting to that effect. Stops cleanly before touching the
  wallet when the amount is below what the method can settle, or when no payment method
  is shared with the peer.  
  **Example:**  
  `sudo nodo pay <peer_id> 0.01 --payment-method bitcoin:BTC`  
  `sudo nodo pay <peer_id> 25 --payment-method ergo:SigUSD`

- **donations**  
  What this node donates and whose donations it counts. Prints the share of earnings
  being donated, what has been paid out, what is accrued and not yet paid, both wallet
  lists with their weights, and the donation credit each peer currently earns in the
  balancer. It also names any address that is in one list and not the other — funding a
  developer this node does not count costs money and earns it no credit of its own.
  `--json` is the report the TUI's EARNINGS page reads. Read-only: nothing is signed or
  spent. See [DONATIONS.md](DONATIONS.md).  
  **Example:**  
  `nodo donations`

- **submit_reputation**  
  Forces the submission of reputation information. Writes the resulting proof id to
  `config.yaml`, so it restarts a serving node (needs root — see
  [Changing configuration on a running node](#changing-configuration-on-a-running-node)).  
  **Example:**  
  `sudo nodo submit_reputation`

- **sync_reputation_proof**  
  Reconciles the locally configured reputation proof with the wallet mnemonic and reports
  every step. It (1) validates the currently configured proof, (2) removes it from the
  config if it is not owned by the configured wallet, (3) if a mnemonic is configured,
  looks up an on-chain reputation proof owned by that wallet and stores its id in the
  config when one exists. Run it by hand after changing the mnemonic, so the new wallet
  picks up its associated reputation proof (if any). Steps 2 and 3 write `config.yaml`,
  so it restarts a serving node (needs root — see
  [Changing configuration on a running node](#changing-configuration-on-a-running-node)).  
  **Example:**  
  `sudo nodo sync_reputation_proof`

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

### Changing configuration on a running node

`config.yaml` is read **once, at start**. Nothing watches the file, so a running node
keeps serving the configuration it booted with no matter what the file says afterwards.
Everything the node derives from a config value — its identity keypair, the TLS
certificate peers pin, the interpolated paths — is fixed for the life of the process,
which is exactly the point.

So change settings through `nodo tui` or `nodo config set` (scriptable — see
[`config set`](#new-commands-for-tui-parity)), which write the file and restart the node
as one transaction (and revert the file if the node does not come back).

The same rule binds nodo's own commands: a CLI command that writes `config.yaml`
(`nodo sync_reputation_proof`, `nodo submit_reputation`) restarts a serving node itself
once it sees the file changed, which is why those need root on a running node. If the
restart cannot happen, the command says so and tells you to run it — the write is on
disk but not live until you do.

If you edit `config.yaml` by hand, restart the node yourself:

```bash
sudo nodo daemon restart
```

Until you do, the running node ignores the edit, and the next value it persists rewrites
the file from what it loaded — overwriting the change. Hand-edit a stopped node. Details
in [`CONFIG.md`](CONFIG.md).

### Manual Execution in Development Mode: `nodo serve`

Use `nodo serve` to run Nodo in a development environment or when you don’t want to use background service mode.
If `hashing.CHECK_INTEGRITY_ON_SERVE` is set to `true`, Nodo runs an automatic integrity/migration check before starting.

---

## Scripting and AI agents 🤖

### Can an AI agent use the TUI?

**No, not reliably — and it does not need to.** `nodo tui` is a full-screen ratatui
application: it needs a real terminal (PTY), redraws the whole screen every two
seconds, lays itself out differently at every terminal size, answers keys and
mouse clicks rather than arguments, prints no structured output, and has no exit
status that says whether an action worked (outcomes appear as a one-line status
message at the bottom of the screen). An agent could drive it only by emulating a
terminal and scraping the screen, which is slow, fragile and unverifiable.

Everything the TUI can *show* or *do* is therefore also a plain command, listed in
the [TUI ↔ CLI mapping](#tui--cli-mapping) below. Most of the TUI's actions already
were commands — it runs `nodo kill`, `nodo connect`, `nodo credit_client`,
`nodo chat_*`, `nodo reputation --json`, … in the background — and the rest
(configuration editing, profiles, the Overview, Earnings, Energy and Schedule
pages, peer reputation adjustment, the detail cards) are commands now too.

### Conventions

- **Exit status** — `0` when the command did what it says, `1` when it refused or
  failed. Read-only commands exit `0` when they produced their report, even if the
  report says something bad (e.g. `nodo status` on a stopped node: read `serving`).
- **`--json`** — prints exactly **one JSON object on one line** to stdout, stamped
  with `read_at` (Unix seconds). A failure is still one object: `{"error": "...",
  "read_at": ...}` with exit status `1`. MU amounts are integers in fields ending
  in `_mu` (they can exceed 2^53 — parse them as big integers if your language
  needs it); `*_display` fields are the same amount rendered in `ui.DISPLAY_UNIT`.
  A figure that cannot be measured is `null`, never `0`.
- **No prompts.** The only interactive questions are the first-run KyA and
  donation questions (see "Non-interactive use" at the top of this page: create
  `storage/.acceptedkya` and set `NODO_DONATION_PERCENTAGE`) and
  `nodo burnall`, which needs `--yes`.
- **Root.** Commands that restart the node (`config set|append|remove`,
  `config profile --apply` on a serving node, `daemon …`) or touch VMs (`kill`,
  `remove`, `prune`) need root, exactly as in the TUI.
- **`--limit N`** bounds the history rows in a detail view (default 50; the TUI
  shows 8).

Commands with `--json`: `status`, `services`, `instances`, `peers`, `protocol`, `clients`,
`peer_reputation`, `config` (all subcommands), `earnings`, `energy`, `schedule`,
`logs -n`, `docs`, `chat <peer>` (reading), `chat_open`, `chat_threads`,
`chat_thread`, `reputation`, `donations`, `resources`, `tx_history`, `kill`, `tunnel`,
`tunnels`, `tunnel_close`.

### Agent quick start

```bash
nodo status --json                         # is it serving? alerts? counts?
nodo instances --json                      # what is running, with live counters
nodo peers --json                          # who we talk to
nodo peers <peer_id> --json                # + payments and reputation events
nodo config get pricing --json             # read any config subtree
sudo nodo config set network.DELEGATE_EXECUTION=true --json   # atomic, restarts, rolls back
nodo config profile --json                 # which posture is this node closest to?
nodo logs -n 100 --json                    # last 100 log lines, then exit
nodo tunnel <instance> 8080 --detach --json  # reach a slot from here; returns its id
nodo tunnels --json                        # tunnels running on this host
nodo tunnels --instance <instance> --json  # the ones reaching one instance
nodo tunnels --inbound --json              # streams this node relays for others
nodo tunnel_close <tunnel id> --json       # and close one
```

### New commands for TUI parity

- **status `[--json] [--wallet] [--storage]`**
  The TUI's OVERVIEW page as one report: whether a node is serving on the gateway
  port, version (git commit), node id, gateway port, the address peers are told,
  reputation proof id, `ACTION REQUIRED` alerts, catalogue counts, memory/disk
  reserved by local instances, and host CPU/load/memory/disk. `--wallet` adds the
  payment-contract balances (starts a JVM; slow), `--storage` sizes the storage
  directory (walks it; slow). Bare `nodo` prints the same in prose.
  ```json
  {"serving": true, "version": "d757bafc…", "node_id": "02ab…", "gateway_port": 8090,
   "address": "203.0.113.5:8090", "scope": "public", "reputation_proof_id": "…",
   "alerts": [{"key": "gateway_port", "summary": "…", "detail": "…", "line": "ACTION REQUIRED …"}],
   "counts": {"local_instances": 2, "delegated_instances": 0, "peers": 5, "clients": 3,
              "services": 7, "reserved_mem_bytes": 2147483648, "reserved_disk_bytes": 10737418240},
   "host": {"cpu_count": 16, "load_avg": [0.4, 0.3, 0.2], "mem_total_bytes": …,
            "mem_available_bytes": …, "disk_total_bytes": …, "disk_free_bytes": …},
   "read_at": 1790000000}
  ```

- **config get `[<path>] [--json] [--show-secrets]`**
  Reads `config.yaml` **as it is on disk** (not the interpolated view `nodo envs`
  prints). Paths are dotted with `[n]` for list elements: `network.GATEWAY_PORT`,
  `core_services[1].id`. No path prints the whole file. Text output is one
  `path: value` line per leaf (values JSON-encoded), as the CONFIG page lists them.
  Secret-looking paths (`mnemonic`, `password`, `secret`, `private_key`, `api_key`,
  `token`) are masked as `********` unless `--show-secrets`.
  JSON: `{"path": "pricing", "value": {...}}`.

- **config set `<path>=<value> [<path>=<value> ...] [--json]`**
  Writes one or more keys **in one transaction** — the same one every TUI page
  uses: snapshot `config.yaml` to `config-<UTC stamp>-<nnnn>.yaml` (ten kept),
  write with nodo's `yq` in place (comments kept; all keys in one `yq` call),
  then, if a node is serving, `nodo daemon restart` and wait (up to 120 s) for the
  gateway port to answer; **if it does not come back, the snapshot is restored and
  the node restarted on it**. Values are YAML, so they keep their type: `true`,
  `5000`, `1.5`, `null`, `[]`, `["a", "b"]`, `'"quoted"'`. On a serving node this
  needs root (it is refused otherwise, and nothing is written). A changed gateway
  port drops its "proven reachable" marker so the next start re-probes it.
  ```bash
  sudo nodo config set pricing.SCARCITY_CURVE=2.0 host_limits.ENABLED=true
  nodo config set activity_window.ENABLED=true 'activity_window.WINDOWS=[{"START": "08:00", "END": "18:00"}]'
  ```
  JSON: `{"ok": true, "label": "Set …", "outcome": "restarted" | "not-running" |
  "refused" | "reverted", "backup": "/…/config-20261002140501-0042.yaml",
  "error": null, "values": {"pricing.SCARCITY_CURVE": 2.0}}`.

- **config append `<list path> <value> [--json]`** / **config remove `<path>[<n>]` `[--json]`**
  Add an element to a list, or remove one element (only list elements can be
  removed — a key is set, never deleted, so the node never silently falls back to
  a default). Same transaction as `set`.
  ```bash
  nodo config append service_networks.blacklist '"*.example.com"'
  nodo config remove service_networks.blacklist[0]
  ```

- **config profile `[<profile>] [--apply] [--json]`**
  The POLICIES page's postures — `just-me`, `cautious`, `open-renter`, `lan-lab`,
  `workbench` (most closed to most open). No argument lists them with how far this
  node is from each and which is closest (ties go to the more closed one). A
  profile name lists exactly which keys differ (`from` → `to`); `--apply` writes
  those keys in one transaction. A profile writes policy only — never an identity,
  wallet, path or port. The catalogue is the TUI's (`cell.rs`); a test keeps the
  two identical.
  JSON (list): `{"closest": "cautious", "profiles": [{"id", "label", "blurb",
  "total", "deviations": [{"path", "from", "set", "to"}]}]}`.

- **peers `[<peer_id>] [--json] [--limit N]`**
  No id lists every peer as before (now with its endpoints). An id narrows to one
  peer and adds the PEERS detail card: every payment made to it and the reputation
  events behind its score. JSON: `{"peers": [peer, …]}` or `{"peer": peer}` where
  `peer` is `{"id", "endpoints", "protocol_stack", "remote_client_id",
  "local_client_id", "balance_peer_mu", "balance_mu", "balance_display",
  "balance_last_update", "payment_methods": [{"ledger_tag", "contract_hash",
  "address", "token_id", "mu_per_unit"}], "advertised_rates", "reputation_proofs",
  "reputation_score", "reputation_index", "last_index_on_ledger"}` plus, for one
  peer, `"payments": [{"created_at", "direction", "amount_mu", "status", "ledger",
  "tx_id", "deposit_token"}]` and `"reputation_events": [{"created_at", "amount",
  "reason", "score_after"}]`.

- **peer_reputation `<peer_id> <+N|-N> [--json]`**
  The TUI's `+`/`-` on PEERS: moves this node's local score of a peer by N and
  records an `operator_adjustment` reputation event in the same transaction.
  JSON: `{"peer_id", "delta", "reputation_score", "reputation_index"}`.

- **clients `[<client_id>] [--json] [--limit N]`**
  No id lists every client as before (now with whether it is metered). An id adds
  the CLIENTS detail card: what it paid us, its deposit tokens, the instances it
  started here, and the peer it is bound to, if any. JSON: `{"clients": [...]}` /
  `{"client": {"id", "balance_mu", "balance_display", "last_usage", "unmetered",
  "payments", "deposit_tokens", "instances", "bound_peer_id"}}`.

- **services `[<service>] [--json] [--limit N]`**
  No argument lists the registry as before. A service id or tag adds the SERVICES
  detail card: its reputation score here (across every instance of it that ever
  ran) and the events behind it. JSON: `{"services": [{"id", "tag", "size_bytes",
  "stored_bytes", "size_error"}]}` / `{"service": {…, "reputation_score",
  "reputation_events"}}`.

- **instances `[<search>] [--grouped] [--json]`**
  `--json` prints `{"instances": [...]}`: the printed fields plus raw values —
  `service_id`, `balance_mu`, `mem_limit_bytes`, `disk_space_bytes`, `uris`
  (`[{"ip", "port", "internal_port"}]`), `runtime` (pid, uptime, RSS, cgroup memory)
  and `usage`: cumulative `cpu_usage_usec`, `memory_current_bytes`, `net_rx_bytes`,
  `net_tx_bytes`, read from the instance's cgroup and tap exactly as the INSTANCES
  page reads them. These are counters, not rates: the TUI's CPU% is the
  `cpu_usage_usec` delta between two reads divided by the elapsed microseconds
  (not normalised by vCPUs). Delegated instances have no local counters (`null`).
  Use `parent_id` to rebuild the `--grouped` tree.

- **earnings `[--json]`**
  The EARNINGS page's money half: what each payment network brought in over the
  last day/week/month/year and all time (accepted incoming payments only; refused
  deposits are counted apart). The reputation half is `nodo reputation --json`.
  JSON: `{"earnings": [{"ledger": "ergo", "total_mu", "day_mu", "week_mu",
  "month_mu", "year_mu", "refused_mu"}]}`.

- **energy `[--json] [--hours N]`**
  The ENERGY page: the latest `energy_consumption` sample, that table folded into
  local hours over the last N hours (default 720), and the `energy:` config block.
  JSON: `{"latest": {"timestamp", "watts", "price_per_kwh", "currency", "backend",
  "is_floor"}, "hourly": [{"hour": "2026-10-02T14", "peak_watts", "joules", "cost"}],
  "config": {...}, "hours": 720}`. Change the settings with `nodo config set energy.…`.

- **schedule `[--json] [--days N]`**
  The SCHEDULE page: whether the working hours are enforced, open right now, the
  windows, what closing time does, and `demand_history` folded onto the 24 hours of
  a clock over the last N days (default 30). JSON: `{"config": {...}, "enabled",
  "open_now", "windows": [["08:00", "18:00"]], "stops_running_instances", "days",
  "demand_by_hour": {"held": [24 ints], "refused": [24 ints]}}`. Change it with
  `nodo config set activity_window.…` (all three keys in one call, so the node is
  never restarted onto half a schedule).

- **logs `[-n <lines>] [--json]`**
  Bare `nodo logs` follows the log forever (`tail -f`). `-n N` prints the last N
  lines and exits; `--json` prints `{"path", "lines": [...]}` (200 lines if no `-n`).

- **docs `[<page>] [--json]`**
  The DOCS page: lists every Markdown page under this installation's `docs/` with
  its title, or prints one page's Markdown (`nodo docs CONFIG`, `nodo docs
  proposals/x.md`). `NODO_DOCS_DIR` overrides the folder. JSON: `{"root", "pages":
  [{"page", "title"}]}` / `{"page", "title", "markdown"}`.

- **chat `<peer>` / chat_threads `[<peer>]` / chat_thread `<id>` / chat_open … `--json`**
  The CHAT page's reads as JSON: `{"peer_id", "messages": [...]}`,
  `{"conversations": [{"id", "peer_id", "opened_by_us", "topic", "opened_at",
  "closed_at"}]}`, `{"conversation_id", "messages": [{"ts", "from_us", "body",
  "conversation_id", "service"}]}`; `chat_open --json` returns the new
  `{"conversation_id", "peer_id", "topic"}` so the next `chat_reply` can use it.

### TUI ↔ CLI mapping

| TUI page / action | Key | CLI equivalent |
|---|---|---|
| KyA gate (first run) | `y` / `n` | first interactive `nodo` run, or `touch <MAIN_DIR>/storage/.acceptedkya` |
| OVERVIEW: status, version, node id, address, proof id | — | `nodo status [--json]` (new); bare `nodo` |
| OVERVIEW: ACTION REQUIRED banner | — | `nodo status --json` → `alerts` (new); `nodo doctor` |
| OVERVIEW: host CPU/RAM/disk, reserved resources, counts | — | `nodo status --json` → `host`, `counts` (new) |
| OVERVIEW: storage size | — | `nodo status --storage` (new) |
| OVERVIEW: wallet balances | — | `nodo status --wallet` (new); bare `nodo` |
| OVERVIEW: peers' announced resources / own announcement | — | `nodo resources --json`; `nodo peers --json` |
| OVERVIEW: earnings / donations summary | — | `nodo earnings`, `nodo donations --json` |
| OVERVIEW: energy / schedule summary | — | `nodo energy`, `nodo schedule` (new) |
| INSTANCES: table, live CPU/RAM/net | — | `nodo instances --json` (new `--json`, live counters); `nodo observe <id>` (interactive stream) |
| INSTANCES: dependency tree | `g` | `nodo instances --grouped`; `parent_id` in `--json` |
| INSTANCES: kill (closes its tunnels too) | `k` | `nodo kill <instance> [--json]` (new: closes its tunnels; `--json`) |
| INSTANCES: open a tunnel to the selected instance (y/N with the fee) | `t` | `nodo tunnel <instance> <slot> [--listen <port>] [--udp] --detach [--json]` (new `--detach`; fee in `open_fee_mu`) |
| INSTANCES: tunnels of the selected instance (card line + table under it) | — | `nodo tunnels --instance <instance> [--json]` (new); `nodo instances --json` → `tunnels` |
| TUNNELS: table, card | — | `nodo tunnels [--json]` (new) |
| TUNNELS: details + log tail | `i` | `nodo tunnels <tunnel id> [--json]` (new) |
| TUNNELS: open a tunnel to any instance (y/N with the fee) | `n` | `nodo tunnel <instance> <slot> [flags] --detach` (new `--detach`; fee in `open_fee_mu`) |
| TUNNELS: close | `d` | `nodo tunnel_close <tunnel id>` (new) |
| TUNNELS: INBOUND table (streams relayed for others; list only) | — | `nodo tunnels --inbound [--json]` (new) |
| SERVICES: list | — | `nodo services [--json]` (new `--json`) |
| SERVICES: reputation card | — | `nodo services <service> [--json]` (new) |
| SERVICES: details | `i` | `nodo inspect <service>` |
| SERVICES: execute | `e` | `nodo execute <service>` |
| SERVICES: delete | `d` | `nodo remove <service>` |
| SERVICES: get from peers | — | `nodo get <service>` |
| SERVICES / PACKS: pack a folder or an https git URL | `p` / `n` | `nodo pack <dir \| https URL[#subdir]> --detach [--json]` (new `--detach`, `--json`) |
| PACKS: table, card (current and recent packs) | — | `nodo packs [--active] [--json]` (new) |
| PACKS: details + log tail | `i` | `nodo packs <pack id> [--json]` (new) |
| PACKS: cancel (y/N) | `c` | `nodo pack_cancel <pack id> [--json]` (new) |
| PEERS: table | — | `nodo peers [--json]` (new `--json`) |
| PEERS: payments + reputation events card | — | `nodo peers <peer> [--json]` (new) |
| PEERS: connect | `c` | `nodo connect <host:port>` |
| PEERS: forget | `d` | `nodo disconnect <peer>` |
| PEERS: adjust local reputation | `+` / `-` | `nodo peer_reputation <peer> +1` / `-1` (new) |
| CLIENTS: table | — | `nodo clients [--json]` (new `--json`) |
| CLIENTS: payments / tokens / instances / bound peer card | — | `nodo clients <client> [--json]` (new) |
| CLIENTS: credit / debit | `+` / `-` | `nodo credit_client` / `nodo debit_client <client> <amount>` |
| CHAT: threads (ours / theirs) | `←` / `→` | `nodo chat_threads [<peer>] [--json]` (`opened_by_us`) |
| CHAT: thread messages | — | `nodo chat_thread <id> [--json]` |
| CHAT: open / reply / close / reopen | `o` / Enter / `c` / `R` | `nodo chat_open`, `chat_reply`, `chat_close`, `chat_reopen` |
| EARNINGS: money per network and window | — | `nodo earnings [--json]` (new) |
| EARNINGS: reputation staked, proofs | `r` | `nodo reputation [--json]` |
| EARNINGS: donations | — | `nodo donations [--json]` |
| POLICIES: closest profile, deviations | `d` | `nodo config profile [<profile>] [--json]` (new) |
| POLICIES: apply a profile | `p` | `nodo config profile <profile> --apply` (new) |
| POLICIES: move a lever / edit its keys | Enter / `e` | `nodo config set <key>=<value> …` (new; one call per lever, all its keys) |
| POLICIES: Ergo accepted tokens add/remove | `a` / `d` | `nodo config append ledgers.ergo.payments.ASSETS '{…}'` / `nodo config remove ledgers.ergo.payments.ASSETS[n]` (new) |
| POLICIES: router steps | `n` | `nodo nat-guide` |
| PRICING: prices, nudge ±10 % | `+` / `-`, `e` | `nodo config get pricing --json` / `nodo config set pricing.…=<value>` (new) |
| ENERGY: settings | `e`, Enter | `nodo config get energy` / `nodo config set energy.…` (new) |
| ENERGY: history chart, today/7d/30d | — | `nodo energy [--json] [--hours N]` (new) |
| SCHEDULE: windows, on/off, closing behaviour | `a`/`d`/`w`/`c`/arrows/Enter | `nodo config set activity_window.ENABLED=… activity_window.WINDOWS=… activity_window.ON_CLOSE=…` (new) |
| SCHEDULE: demand by hour, refused while closed | — | `nodo schedule [--json] [--days N]` (new) |
| CONFIG: browse / filter the tree | arrows, `/`, `x` | `nodo config get [<path>] [--json]` (new); `nodo envs` (interpolated) |
| CONFIG: edit a value | `e` | `nodo config set <path>=<value>` (new) |
| CONFIG: add / remove list element | `a` / `d` | `nodo config append` / `nodo config remove` (new) |
| Any config change: backup → restart → rollback | — | built into every `nodo config` write (new) |
| LOGS: tail of app.log | — | `nodo logs -n <lines> [--json]` (new `-n`) |
| LOGS: output of actions launched from the TUI | — | each command's own stdout and exit status |
| DOCS: index, read a page | arrows, Enter | `nodo docs [<page>] [--json]` (new) |
| DOCS: search, follow links | `/`, `n`, `l`, Enter | `grep -r` over the `docs/` files |
| Refresh | `r` | re-run the command |
| Theme | `--theme` | n/a — presentation only |

Not ported, deliberately: themes, layout and mouse handling (presentation only);
in-page search and link following on DOCS (an agent reads the Markdown directly);
the POLICIES page's *lever catalogue* — the named one-row decisions and their
explanations. Every lever is a set of config keys, so `nodo config set` can put a
node in any state a lever can, but the human-readable names and wording live only
in `cell.rs`. The profile catalogue *is* ported, because "which posture is this
node in" is a question an agent needs answered.

### Complete command index

Every command `nodo help` lists, in one place (details elsewhere on this page):

| Command | What it does |
|---|---|
| `nodo` | quick start, status, address, wallet, alerts (prose) |
| `help` | the command catalogue |
| `tui` | the interactive operations console (humans) |
| `status [--json] [--wallet] [--storage]` | the TUI's OVERVIEW as data |
| `doctor` | check, and fix, what stops the node serving |
| `daemon start\|status\|stop\|restart` | control the `nodo.service` unit (root) |
| `logs [-n <lines>] [--json]` | follow the log, or print a bounded tail |
| `envs` | print the effective (interpolated) config.yaml |
| `config get\|set\|append\|remove\|profile` | read and change config.yaml transactionally |
| `schedule [--json] [--days N]` | working hours and demand by hour |
| `energy [--json] [--hours N]` | power drawn and what it cost |
| `docs [<page>] [--json]` | list or print this installation's docs |
| `nat-guide` | the router steps to be reachable from the Internet |
| `firewall-compat status\|apply\|remove` | rules a coexisting firewall must keep |
| `completion bash\|zsh\|install` | shell tab-completion |
| `update` | update nodo itself (root) |
| `pack <dir\|git url>` | package a project into a service |
| `download <url> [-o <dir>]` | fetch a published service from its manifest |
| `get <service> [--now]` | ask peers for a service not held locally |
| `import <path>` / `export <service> <path> [--raw]` | read in / write out a `.celaut` file |
| `publish <service>` | offer a service to the network |
| `services [<service>] [--json]` | list the registry, or one service's reputation |
| `inspect <service>` | a service's spec and metadata |
| `tag <service> <tag>` | name a service |
| `remove <service>` | drop a service from the registry (root) |
| `integrity [<service>] [--fix]` | check stored blocks against their hashes |
| `estimate <service>` | what an execution would cost |
| `execute [--name n] [-e k v] <service>` | run a service |
| `instances [<search>] [--grouped] [--json]` | what is running |
| `observe <instance> [--save <path>]` | live metrics and network capture (streams; Ctrl+C) |
| `tunnel <instance> <slot> [...] [--detach] [--json]` | reach an instance's port from here (until stopped; `--detach`: in the background) |
| `tunnels [<tunnel> \| --instance <i> \| --inbound] [--json]` | the tunnels running on this host, one with its log, those reaching an instance, or the streams relayed for others |
| `tunnel_close <tunnel>... \| --all [--json]` | stop tunnels |
| `kill <instance> [--json]` | stop one instance and close its tunnels (root) |
| `burnall [--yes]` | stop every instance, parents first |
| `prune [--all] [--dry-run]` | reclaim orphaned runtime dirs |
| `peers [<peer>] [--json] [--limit N]` | peers, or one with its history |
| `peer_reputation <peer> <+N\|-N> [--json]` | move our local score of a peer |
| `resources [--json]` | what this node announces it can run |
| `connect <host:port>` / `disconnect <peer>` | introduce / forget a peer |
| `chat <peer> [message...] [--service s] [--json]` | read or send the flat chat |
| `chat_open <peer> <topic...> [--message m] [--service s] [--json]` | start a conversation |
| `chat_reply <id> <message...> [--service s]` | reply in a conversation |
| `chat_threads [<peer>] [--json]` / `chat_thread <id> [--json]` | list conversations / read one |
| `chat_close <id>` / `chat_reopen <id>` | close / reopen (local only) |
| `clients [<client>] [--json] [--limit N]` | clients, or one with its history |
| `reputation [<peer>] [--json]` | what the network stakes on us (or on a peer) |
| `verify_reputation <peer>` | validate a peer's reputation proof and ownership |
| `submit_reputation` / `sync_reputation_proof` | publish / reconcile this node's proof |
| `earnings [--json]` | money taken in, per network and window |
| `donations [--json]` | who we fund, who we count |
| `increase_deposit` / `decrease_deposit <instance> <amount>` | fund / defund a running instance |
| `increase_peer_deposit <peer> <amount>` | top up what a peer holds for us |
| `credit_client` / `debit_client <client> <amount>` | change a client's balance here |
| `pay <peer> <amount> [--payment-method l:a] [--ledger] [--asset]` | pay a peer on-chain |
| `tx_history [--json]` | payments made and received |
| `serve` | run the node in the foreground (development) |
| `migrate` | recreate the database from scratch |
| `test <name>` | run one test from `tests/` |
| `ggconf <dir> [-e k v]` | gateway config for a local project |
| `force_execution <peer> <service>` | delegate to one peer, no balancer (testing) |
| `local_builder <buildctl args>` | talk to nodo's rootless BuildKit builder |
| `storage:prune_blocks` | drop blocks nothing references |
| `prune_containers` | sweep dead VMs now (root) |
| `refresh_clients` | settle client and peer deposits now |
| `refresh_ergo_nodes` | refresh the list of Ergo nodes |

---

## Terminal User Interface (TUI)

Run `nodo tui` to open the operations console. Its pages cover node/host statistics, current
instance resource usage and reservations, local services, peers, clients, what the node has
earned, complete `config.yaml` editing, logs, storage, Ergo wallet balances, and this
documentation (the DOCS page). The TUNNELS page lists the `nodo tunnel` processes
running on this host.

- `Tab`/`Shift+Tab` switches pages; Up/Down selects rows.
- `r` refreshes.
- On Peers, `c` connects a peer, `d` forgets the selected one (`nodo disconnect`), and `+`/`-` adjust its
  reputation. The detail card shows what this node has paid that peer and the events behind its score.
- On Clients, `+`/`-` credit/debit the selected client's balance (`nodo credit_client`/`debit_client`),
  and the detail card shows what a client has paid, its deposit tokens, and the instances it
  started here.
- On Earnings, the money each payment network brought in is windowed — last day, week,
  month, year and all time — because money is a flow; what the network stakes on this
  node is shown as a standing with the ERG sunk behind it, and deliberately not windowed,
  because a proof re-dates every one of its opinions when it republishes (see
  [ERGO.md](ERGO.md#why-there-is-no-reputation-earned-this-week)). Every proof that has
  staked something on this node is listed underneath. Money comes from the catalogue and
  is live; the reputation half is `nodo reputation` re-read every five minutes, and `r`
  re-reads it now. Between them sits what *leaves*: the share of earnings this node
  donates, what is still accrued, and the wallets it funds and counts, from
  `nodo donations` re-read every ten minutes. A peer's own donation credit, and the
  bonus it earns in routing, are on its card on the Peers page.
- On Instances, `t` opens a tunnel to the selected instance: type the slot, plus any of
  `--listen <port>`, `--udp`, `--host`, `--peer`, `--idle`. It runs
  `nodo tunnel <instance> <slot> … --detach` after a y/N that states the fee (each
  connection spends `pricing.TUNNEL_OPEN_MU` of the instance's balance), and the
  status line says where it listens.
  The card counts the tunnels already reaching that instance, and a table under it
  lists them (listen, slot, id, via, age) — `nodo tunnels --instance <instance>`.
- On Tunnels, `n` opens a tunnel to any instance (`<instance> <slot> [flags]`), `d`
  closes the selected one after a confirmation (`nodo tunnel_close`), and `i` shows it
  with the tail of its log. The INBOUND table under the card lists the streams this
  node relays for others (`nodo tunnels --inbound`); it is read-only.
- On Services, `e` executes the selected service and `d` deletes it.
- On All, Right/Left enter and leave a branch of the tree, `e` edits any selected YAML
  value, `/` filters values, and `x` clears the filter. Secrets are masked, comments are
  preserved, and each write snapshots the previous file to
  `config-<timestamp>-<nnnn>.yaml` — one snapshot per write, not per second.
- On Policies, the node's policies are grouped into sections (Network, Workload, Publishing, Identity, Security, Resources, Payments, Storage): Right/Left move between sections,
  Up/Down between the decisions inside one, and Enter moves a decision to its next position
  (after showing every key it would change). `p` applies a whole posture — "just me",
  "cautious renter", "open renter", "lan lab", "workbench" — and `d` shows exactly where
  this node differs from the one it is closest to. `n` prints the router steps.
- **Every configuration change from the TUI backs up `config.yaml`, writes it, and restarts
  nodo — and puts the backup straight back if the node does not come up on it.** So the
  file always describes the node that is running, and no change is left waiting for a
  restart somebody has to remember. This is why the TUI is the supported editor: the node
  reads `config.yaml` once at start and never again, so a change that is not restarted
  into is a change the node never sees. The restart drives `systemctl`, so editing
  configuration on a serving node needs root.
- On Docs (`6`), the `docs/` folder is indexed on the left and the selected page rendered
  on the right: `/` searches it, `l` and Enter follow its links to other pages, and
  Backspace comes back.
- `q`, Escape, or Ctrl+C exits.

See [the TUI reference](../src/commands/tui/README.md) for page details, refresh behavior, and
the typed configuration editor contract.

---

## Shell Completion

Nodo ships `<Tab>` completion for **bash** and **zsh**. It completes command names and, for the
commands that take one, the identifier of the relevant object:

- **Service id or tag** — `execute`, `estimate`, `inspect`, `remove`, `publish`, `tag`,
  `export`, `integrity`, `get`, `services`
- **No argument** — `prune` (flags only: `--all`, `--dry-run`)
- **Instance id or name** — `kill`, `observe`, `tunnel`, `increase_deposit`, `decrease_deposit`
- **Peer id** — `disconnect`, `increase_peer_deposit`, `peers`, `peer_reputation`, `reputation`,
  `verify_reputation`, `pay`, `chat`, `chat_open`, `chat_threads`, `force_execution`
- **Client id** — `credit_client`, `debit_client`, `clients`
- **Tunnel id** — `tunnels`, `tunnel_close`
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
