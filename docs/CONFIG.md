# Configuration Reference (`config.yaml`)

Nodo reads a single `config.yaml`, created from
[`config.example.yaml`](../config.example.yaml) at install time. It lives in the
installation root (`TARGET_DIR`, default `/nodo`), i.e. `/nodo/config.yaml`. The
`main.MAIN_DIR` value inside it is the same root.

## Edit it with `nodo tui`

The Config page is the supported way to change a value, because it is the only one
that does all four things a change needs:

1. **Validates** the value against the key's type before it lands.
2. **Backs up** the previous file to `config-<YYYYMMDDHHMMSS>-<nnnn>.yaml` beside it.
3. **Writes** in place with `yq -i`, preserving comments.
4. **Restarts nodo** — and puts the backup straight back if the node does not come
   up on the new file.

The four are one transaction, so the file always describes the node that is running,
and a change that cannot be started into is undone rather than left on disk. The Cell
page is the same mechanism at a coarser grain: it groups these keys into the decisions
an operator actually makes, and one of its levers or profiles writes several keys as a
single change. See [the TUI reference](../src/commands/tui/README.md#applying-a-change).

> ⚠️ **`config.yaml` is read once, when the node starts.** Nothing watches the file.
> A running node keeps serving the configuration it booted with, however many times
> the file changes underneath it. That is deliberate: everything derived from a config
> value — the identity keypair, the TLS certificate peers pin against this node's
> `peer_id`, the interpolated paths — is then fixed for the life of the process
> instead of drifting out of step with it mid-run.
>
> So if you edit the file by hand, **restart the node yourself**:
>
> ```bash
> sudo nodo daemon restart
> ```
>
> Until you do, the edit is invisible to the running node — and worse, it is not
> safe: nodo rewrites the whole file from the configuration it loaded whenever it
> persists a value of its own (`ledgers.ergo.NODE_URL`, a reputation proof id), which
> silently reverts a hand edit that has not been restarted into. There is no
> validation and no backup on that path either. Hand-editing is for a node that is
> stopped.

## What writes `config.yaml`

Four paths, and every one of them snapshots the file to
`config-<YYYYMMDDHHMMSS>-<nnnn>.yaml` before overwriting it (the ten most recent are
kept). The stamp is UTC and the four trailing digits are random, so two writes inside
the same second get a snapshot each instead of the second overwriting the first's:

| Writer | What it writes | Restart |
|---|---|---|
| `nodo tui` | whatever you edit, in one `yq` invocation | Restarts the node, and reverts the file if it does not come back. |
| A CLI command — `nodo sync_reputation_proof`, `nodo submit_reputation` | `ledgers.ergo.reputation.REPUTATION_PROOF_ID` | Restarts a serving node once it sees the file changed. Needs root; if the restart cannot happen the command says so and names the fix. |
| The daemon itself | `ledgers.ergo.NODE_URL` when the configured Ergo node stops answering, and the proof id when it submits one | None needed — the process that wrote the value is the one running on it, and the value is live in memory the moment it is set. |
| First load, on any process | resolves `auto` values (`network.GATEWAY_PORT`, `identity.MNEMONIC`, `ledgers.ergo.WALLET_MNEMONIC`) and interpolated paths | None — this happens before the node serves. |

A hand edit is the one write with none of that: no validation, no backup, no restart.

> ⚠️ nodo **rewrites** `config.yaml` on its first load: `auto` values such as
> `network.GATEWAY_PORT`, `identity.MNEMONIC` and `ledgers.ergo.WALLET_MNEMONIC` are resolved to
> concrete values, and the file is re-dumped with `yaml.safe_dump`. This **strips
> all comments**, alphabetizes keys, and persists already-interpolated paths. So a
> live `config.yaml` is uncommented and reordered; `config.example.yaml` remains
> the commented reference.

This page documents the load-bearing keys. `config.example.yaml` is the most
complete reference — when in doubt, read it (though at least one live key,
`packer.PACKER_HEALTH_TIMEOUT`, is read by code but absent from the example).
Values below are the shipped defaults.

> Paths in `config.yaml` may reference other fully-qualified keys such as
> `${main.STORAGE}`; nodo expands them (see the `main` note below). After moving any
> runtime/binary, update the matching `dependencies.*` key and restart
> `nodo.service`.

## `main` — paths

| Key | Default | Meaning |
|---|---|---|
| `main.MAIN_DIR` | `/nodo` | Installation root. |
| `main.STORAGE` | `/nodo/storage` | Node storage root. |
| `main.CACHE` | `${main.STORAGE}/__cache__/` | Build/scratch cache. |
| `main.REGISTRY` | `${main.STORAGE}/__registry__/` | Service specification registry. |
| `main.METADATA_REGISTRY` | `${main.STORAGE}/__metadata__/` | Service metadata. |
| `main.BLOCKDIR` | `${main.STORAGE}/__block__/` | Content-addressed blocks (large files). |
| `main.DATABASE_FILE` | `${main.STORAGE}/database.sqlite` | SQLite database. |

> Interpolation only expands the **fully-qualified**, dot-flattened form
> `${main.STORAGE}` — a bare `${STORAGE}` is left literal. `install.sh`
> (`sync_config_main_paths`) rewrites these `main.*` keys to absolute paths at
> install time, so an installed `config.yaml` contains no placeholders.

The KyA acceptance marker is `${main.MAIN_DIR}/storage/.acceptedkya` — it derives
from `MAIN_DIR`, **not** from `main.STORAGE`, so relocating `STORAGE` does not move
it (see
[`USAGE.md`](USAGE.md#non-interactive-use-automation--agents-️)).

## `dependencies` — local runtimes

Portable runtimes installed under `MAIN_DIR` (not system-wide): `python`, `java`,
`yq`, and `buildkit`. Override only to relocate the toolchain.

`dependencies.buildkit.*` (`BIN`, `DAEMON_BIN`, `BUILDKIT_SOCKET`) is an
**optional, node-local** toolchain used **only** by the local packer
(`packer.local: true`). It is **not** installed at node-install time — nodo runs
`bash/install_buildkit.sh` on demand and drives its own **rootless** builder under
`MAIN_DIR`, never a system-wide daemon. Because the builder runs as the invoking
user, `nodo pack` needs no privileges at all.

## `virtualizers` — execution runtime

Cloud Hypervisor (`ch`) runs everything of the host's own architecture, under
KVM; QEMU (`qemu`) runs the rest, under TCG software emulation. The Docker
virtualizer was removed, so the node needs no local Docker install to *run*
services. Which backend a given service takes is decided per service by
`src/virtualizers/selection.py` — never configured.

| Key | Default | Meaning |
|---|---|---|
| `virtualizers.DEFAULT_VIRTUALIZER` | `ch` | Native backend. The per-service choice is derived, not read from here. |
| `virtualizers.qemu.ENABLE` | `true` | Execute FOREIGN-arch services under emulation. Off = serve only the host's arch. See *Which architectures the node can execute* below. |
| `virtualizers.qemu.BINARY_PATHS` | (from `PATH`) | Per-arch `qemu-system-<arch>`. Empty resolves the well-known name on `PATH`. |
| `virtualizers.qemu.CPU_MODEL` | `max` | QEMU `-cpu` model under TCG. |
| `virtualizers.qemu.GUEST_NETWORK_READY_TIMEOUT_S` | `120` | Emulated boots reach the console far slower than KVM ones; this is the CH timeout's looser twin. |
| `virtualizers.ch.BINARY_PATH` | (set at install) | Cloud Hypervisor binary. |
| `virtualizers.ch.KERNEL_PATHS` / `INITRAMFS_PATHS` | per-arch | Guest kernel/initramfs per `linux/amd64` \| `linux/arm64`. Both are downloaded at install time from the `guest-kernel-vN` release (pinned as `GUEST_KERNEL_VERSION` in `install.sh`): the kernel is not taken from the host's `/boot`, and the initramfs is not built on the host — CI builds it from `bash/build_ch_initramfs.sh`, which is byte-reproducible, so the same commit and busybox reproduce the published image. |
| `virtualizers.ch.NETWORK_MODE` | `tap_bridge` | Guest networking mode. |
| `virtualizers.ch.MIN_MEM_MIB` / `DEFAULT_MEM_MIB` | `128` / `256` | Boot memory floor / default. |
| `virtualizers.ch.SECURITY.*` | — | rootfs path confinement, device-node policy, trusted-service allowlists. |

## Which architectures the node can *execute*

**There is no config key for this.** `SUPPORTED_ARCHITECTURES`
(`src/utils/architectures.py`) is *derived*, from two things the node can check:

1. the **host's own architecture**, which Cloud Hypervisor boots under KVM;
2. plus every **foreign architecture QEMU can emulate here** — which needs
   `virtualizers.qemu.ENABLE` (on by default), the `qemu-system-<arch>` binary,
   and that arch's guest kernel/initramfs on disk. The installer provisions the
   guest assets for *both* architectures and installs the foreign emulator, so a
   default install executes both.

So a node advertises exactly what it can boot, and the way to change that is to
change what is installed — or set `virtualizers.qemu.ENABLE: false` to serve only
the host's own arch. An arch whose emulator or guest assets are missing is
silently not advertised, which is why a node never fails a launch on an arch it
claimed.

This replaces the old `builder.ARM_SUPPORT` / `builder.X86_SUPPORT` pair, which
could disagree with reality in both directions — set to `true` on a host that
could not run that arch, a service was accepted and then died deep inside the CH
build looking for a guest kernel that was never installed. Both keys are now
**rejected**: a config that still carries either one stops the node with a
`ConfigValidationError` (`src/utils/config_validation.py`), so delete them.

Do not confuse any of this with the packer-side pair below
(`packer.ARM_PACKER_SUPPORT` / `X86_PACKER_SUPPORT`), which only affect what
`nodo pack` builds/announces. Those stay explicit flags, and the installer *does*
pin them to the host arch: a local build runs the target's own toolchain and nodo
installs no binfmt handler, so cross-arch *packing* genuinely cannot work.

## `builder` — build tuning

| Key | Default | Meaning |
|---|---|---|
| `builder.WAIT_FOR_UNLOCK_MEMORY` | `60` | Seconds to wait for a memory lock to release during a build (`src/utils/utils.py`). |

## `communication` — peer messaging policy

`communication.*` tunes peer-to-peer messaging behaviour —
`SELF_ANNOUNCE_TO_CONNECTING_PEERS`, `SEND_ONLY_HASHES_ASKING_COST`, and
`DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH` (read by `src/commands/connect.py` and
the execution balancer), plus `MAX_SIGNATURE_SCHEME_COMPONENTS`.

### Where the prose travels

An announcement declares what it means — its signature scheme, and the protocol stack of
each address — in the three fields celaut declares every component with. `formal` and the
tags are what a comparison reads; `prose` is the same thing written out, complete enough
to implement from, at roughly **5 KB per announced address**. Dropping it costs a reader
detail and never costs a verifier its answer, so where it travels is a cost decision:

| Key | Default | Meaning |
|---|---|---|
| `communication.SHARE_PROSE_ON_GET_PEER_INFO` | `false` | Prose in what `GetPeerInfo` serves. Off: the RPC is unauthenticated and answers whoever asks, so the paragraphs are paid on every call to callers the node knows nothing about. On, an announcement is complete enough to implement the protocol from. |
| `communication.SHARE_PROSE_ON_LEDGER` | `false` | Prose in what is written to a reputation box. Off: a box pays storage rent on every byte for as long as it exists. |

The ledger setting also decides what happens to **peers' announcements**, which this node
republishes so a reader can verify their signatures straight off the chain. Those are
republished **exactly as received or not at all**: a peer's prose is inside what it
signed, so editing one to save rent would break the signature that is the whole reason
for carrying it — and a `Peer` that no longer matches what its author signed is not that
peer's claim any more.

So an announcement carrying prose is either published whole (setting `true`, and the rent
paid) or left out. Leaving it out costs only R9: **the opinion is published either way**,
since the opinion is the box's token, its sign and R5. An announcement that carries no
prose is small already and is always republished whole — this decides about expensive
announcements, not about peers.

## `packer` — how `nodo pack` builds

The most important choice for anyone packing services. Full authoring format:
[`PACKING.md`](PACKING.md).

| Key | Default | Meaning |
|---|---|---|
| `packer.local` | `false` | `false` → delegate the build to a **packer-service** microVM (no builder on this host). `true` → build **locally** with nodo's rootless BuildKit toolchain (provisioned on demand, no sudo). |
| `packer.PACKER_SOURCE_URL` | `""` | Manifest URL nodo downloads the packer service from directly when it needs to acquire it. Empty → resolve via the `source-application` core service. |
| `packer.PACKER_SERVICE_URL` | `""` | Override: `ip:port` base URL of an out-of-band packer-service. Used only when no packer id is set / no running instance is found. |
| `packer.ARM_PACKER_SUPPORT` / `X86_PACKER_SUPPORT` | `true` | Architectures `nodo pack` accepts/announces (**packer-side** — to limit what the node can *execute*, use `builder.*` instead). |
| `packer.MIN_BUFFER_BLOCK_SIZE` | `32768` (32 kB) | Local-packer only: inline/block threshold. A file at or above this size is stored as a content-addressed block (`main.BLOCKDIR`); a smaller one is inlined into the service's filesystem message. The main lever on what a service costs in memory — a build streams a pointer straight to its place in the rootfs, so only the inlined part is ever held. Raising it trades memory for fewer, larger block files and faster packs; it never changes a service id. |
| `packer.PACKER_MEMORY_SIZE_FACTOR` | `6.0` | Local-packer only: RAM to lock as a factor of the *inlined* bytes (files under `packer.MIN_BUFFER_BLOCK_SIZE`). Larger files are streamed into blocks and cost no memory. |
| `packer.PACKER_MEMORY_PER_BLOCK` | `10000` | Local-packer only: bytes added per block. With a low `packer.MIN_BUFFER_BLOCK_SIZE` almost every file is a block and nothing is inlined, so this term decides the reservation. |
| `packer.PACKER_MEMORY_OVERHEAD` | `40000000` | Local-packer only: fixed bytes on top — the worker interpreter itself. |
| `packer.WAIT_FOR_UNLOCK_MEMORY` | `300` | Local-packer only: seconds a pack waits for memory before failing instead of waiting indefinitely. |
| `packer.buildkit.DOCKERFILE_NAME` | `Dockerfile` | Local-packer only: name of the Dockerfile inside the project directory. |

The **default-mode** packer is *not* configured here by URL — it is referenced by
its published content hash (service id) in the `core_services` mapping (below), which
is the single source of truth. nodo resolves a running instance of that id and
packs against its `ip:port`.

## `core_services` — bootstrap services (by id)

A mapping of well-known role → published service hash. The node will only
auto-resolve/run a missing service if it is reachable through one of these
configured core services; an empty mapping or a `"<SET_ME>"` placeholder fails
closed ("Service not allowed.").

| Role (key) | Meaning |
|---|---|
| `source-application` | Maps a service id → its downloadable sources (manifest URLs). |
| `packer` | The packer-service used by `nodo pack` (default mode). |
| `low-demand-fallback` | Opportunistic service run only when the node is idle (WIP). |

## `hashing`

| Key | Default | Meaning |
|---|---|---|
| `hashing.HASH` | `sha3_256` | Service/file identification hash. Accepts `sha2_256`, `sha3_256`, `shake_256`, `blake2b_256` (each also its older/shorter alias: `sha256`, `sha3`, `shake`, `blake2b`, `blake2`), or a hex hash-id. |
| `hashing.CHECK_INTEGRITY_ON_SERVE` | `false` | Run integrity/migration automatically on `nodo serve`. |

## `network`

Controls exposure and remote execution. Key entries: `GATEWAY_PORT` (`auto`, TLS —
authenticated against this node's identity key, and the only port announced to peers),
`GATEWAY_PLAINTEXT_PORT` (`auto` = `GATEWAY_PORT + 1`; the same gateway in plain gRPC,
for the services this node runs and for external callers that do not want TLS — `0`
disables it, and then a service must speak TLS too; see [The plaintext
gateway](#the-plaintext-gateway) below),
`PUBLIC_IP` / `EXTERNAL_INTERFACE` (what `nodo execute --remote` advertises),
`PUBLIC_TCP_PORT` / `PUBLIC_UDP_PORT` (the external port a router forwards, when it
differs from the internal one — empty means "same as internal"; only
`PUBLIC_TCP_PORT` is used today, since the gateway is TCP-only),
`FREE_PORTS_RANGE` (ports used to expose services — match your router forwarding),
`DISABLE_EXPOSE_OUTSIDE`, `ISOLATE_INTERNAL_CHILDREN`, and `DEFAULT_EXECUTE_REMOTE`
(default remote for NAT/WSL2 nodes). See also [`NETWORKS.md`](NETWORKS.md).

Service tunneling adds `DELEGATION_TUNNEL_POLICY` (`auto` / `always` / `never`) and
`TUNNEL_UDP_IDLE_TIMEOUT_S` — see [`TUNNELING.md`](TUNNELING.md).

| Key | Default | Meaning |
|---|---|---|
| `network.DELEGATE_EXECUTION` | `true` | Set `false` and this node never asks a peer to run a service for it: the balancer stops polling peers for prices and only ever selects `local`, so a service it cannot run itself fails rather than being delegated. The automatic peer-deposit refill stops too — a deposit buys execution on that peer and nothing else. `nodo pay`, `nodo increase_peer_deposit` and `nodo force_execution` still work, since an operator typing the command overrides the default on purpose. |

The two directions are separate settings, and neither implies the other:

| Want | Set |
|---|---|
| Don't run services **for** other peers (client-only) | `client.ACCEPT_NEW_DEPOSITS: false` |
| Don't ask other peers to run services **for you** (local-only) | `network.DELEGATE_EXECUTION: false` |
| Keep delegating, but approve every outgoing payment yourself | `deposits.AUTOMATIC_REFILL: false` |

### The plaintext gateway

`GATEWAY_PORT` is TLS and is the only port announced to peers; peers and the CLI always
use TLS, with no exception, and this node's own client code has no way to open a
plaintext channel (see [Transport security](CONCEPTS.md#transport-security)).
`GATEWAY_PLAINTEXT_PORT` serves the **same** `Gateway` in plain gRPC for the two callers
that are not peers:

* **The services this node executes.** A service speaks plain gRPC and reaches the node
  over a hop that never leaves the host; it is handed this address as data, in
  `__config__.gateway`, so there is nothing for it to guess. Requiring TLS here would
  mean shipping certificate pinning into every service SDK for a local hop.
* **External callers that do not want TLS.** TLS is what the node *offers*; a caller
  that declines it is that caller's own risk.

It is deliberately hard to reach from elsewhere: it is not announced to peers, no
firewall rule is opened for it, and it listens on one address only — the gateway address
the config file already names (`virtualizers.ch.NETWORK_BRIDGE_NAME`, the same one
written into `__config__.gateway`; loopback if that bridge is not up), never `[::]`.
Serving the unauthenticated `Gateway` on every interface would give away exactly what
the TLS port protects, so reaching it from another host takes a port-forward set up on
purpose.

## `service_networks`

Which communication domains this node is willing to run a service for. A service
declares them as `Service.Network` tags; these two glob lists are the operator's
verdict on that declaration, checked at launch, when quoting a peer, and once more
in the virtualizer.

| Key | Default | Meaning |
|---|---|---|
| `service_networks.blacklist` | `[]` | Tags this node refuses. Checked first, and it wins over the whitelist. `["*"]` refuses every service that declares any tagged network. |
| `service_networks.whitelist` | `[]` | When non-empty, every tag of every declared network must match one of these. |

Both empty — the default — restricts nothing. Patterns are globs matched
case-insensitively against each tag, and glob over the tag only: `google.com` does
not match `www.google.com`, so write `*google.com` for the subdomains too. A
service declaring no network is always accepted. A rejected client is told which
tag, which list and which pattern refused it.

The name is `service_networks`, not `networks`: `network:` above is this node's own
ports and addresses, and a `networks:` block carrying `blacklist`/`whitelist` is
rejected as a config error rather than silently ignored. Full semantics and the
enforcement points: [`NETWORKS.md`](NETWORKS.md).

On the `nodo tui` Config page, `a` appends a pattern to the selected list and `d`
removes the selected one.

## `ddns`

Keeps a hostname pointing at this node's public IP, so peers can find it by name
when the address changes. The manager publishes once at startup and then every
`INTERVAL_SECONDS`.

| Key | Default | Meaning |
|---|---|---|
| `ddns.ENABLED` | `false` | Whether to publish at all. |
| `ddns.PROVIDER` | `desec` | Only `desec` is implemented (`update.dedyn.io`, dyndns2). An unknown value falls back to it. |
| `ddns.DOMAIN` | `""` | Hostname to keep updated, e.g. `my-node.dedyn.io`. |
| `ddns.TOKEN` | `""` | Provider API token. A secret; it never appears in logs or `nodo info`. |
| `ddns.INTERVAL_SECONDS` | `600` | Republish cadence. Invalid values fall back to the default. |

By default **no address is sent** and the provider records the request's source
address — behind NAT that is the only value guaranteed to be right. Set
`network.PUBLIC_IP` to override it (static address, or ingress ≠ egress).

Publishing a name is not the same as being reachable: the router must still
forward the gateway port to this host. Run **`nodo nat-guide`** for the steps with
this machine's addresses filled in; `nodo info` and `sudo nodo doctor` report what
resolves and whether the port is listening. Nothing verifies the forwarding from
*outside* yet — that needs a peer to connect back.

## `energy`

What this machine costs in electricity, and how much of that is each guest. The
manager samples on its own cadence inside the maintenance loop. **Informational
only**: it never touches what the node charges in MU, and it feeds no admission
or `low_demand` decision.

| Key | Default | Meaning |
|---|---|---|
| `energy.ENABLED` | `true` | Whether to sample at all. |
| `energy.SAMPLE_INTERVAL_SECONDS` | `60` | Sampling cadence. Floored at 5. |
| `energy.PRICE_PER_KWH` | `0.0` | Flat tariff. `0` shows watts and no currency cost. Stored with every sample, so a later change does not rewrite history. |
| `energy.CURRENCY` | `"EUR"` | Label only; nothing converts between currencies. |
| `energy.PRICE_SOURCE` | `"fixed"` | Only `fixed` is implemented, and it is the one that works offline. An unknown value falls back to it and says so once. |
| `energy.IDLE_WATTS` | `0` | Watts at 0% CPU, for the model fallback. `0` means uncalibrated and the model then reports nothing. |
| `energy.LOAD_WATTS` | `0` | Extra watts at 100% CPU. `0` falls back to the CPU packages' declared long-term limit where sysfs exposes it. |
| `energy.SMART_PLUG_URL` | `""` | A metering plug's own HTTP endpoint. Empty means no plug and no request. |
| `energy.SMART_PLUG_POWER_PATH` | `"power"` | Dotted path to the watts inside the plug's JSON. |
| `energy.IPMI_ENABLED` | `false` | Ask the BMC through `ipmitool dcmi power reading`. |
| `energy.HWMON_CHIP` | `""` | A `/sys/class/hwmon/hwmonN/name` to read a rail from. |
| `energy.HWMON_SENSOR` | `""` | Sensor prefix in that chip: `power1` (microwatts) or `energy1` (a microjoule counter). |
| `energy.NVML_ENABLED` | `false` | Add the GPUs' draw, through `nvidia-smi`. |
| `energy.EXTERNAL_TIMEOUT_SECONDS` | `2` | Ceiling for each subprocess or HTTP source, per sample. |

### Where the number comes from

Sources are tried in order of how much of the machine each one sees, and the
first that answers wins. The TUI names the one it used, and marks it a `floor`
when the figure is short of the whole machine.

| Source | Sees | Needs |
|---|---|---|
| `smart_plug` | the socket, losses and all | a metering plug and its address |
| `ipmi` | what the power supply pulls in | a BMC, so server hardware |
| `hwmon` | one rail of the board | knowing which rail, from config |
| `rapl` | the CPU packages | nothing, but `energy_uj` is root-only on current kernels |
| `model` | nothing; it estimates | two measured coefficients |

`rapl` is the one most nodes have: `/sys/class/powercap/intel-rapl/`, which the
same driver serves on Intel and on AMD Zen. It is the **CPU package only** — no
GPU, no disks, no power-supply losses — hence the `floor`.

`hwmon` is the reading for machines with no RAPL: Apple Silicon under Asahi
publishes the SMC's rails there (chip `macsmc`), and so do ARM boards with a
shunt. What a rail covers is a property of the board and cannot be guessed, which
is why both the chip and the sensor come from config. Do not point it at the
battery — that measures discharge and reads zero on mains.

`nvml` is not in the order, because a GPU's draw is not an alternative to the
CPU's: it **adds**. It is added only to a figure that is already partial. A plug
and a power supply both see the GPU, and adding it to those would count it twice.

Zero is never a reading. A running machine draws power, so a source answering
`0` is not measuring this machine — a battery on mains, a plug whose socket is
off — and the next source gets its turn instead.

The `model` is the last resort: a straight line, `IDLE_WATTS` at rest plus
`LOAD_WATTS` scaled by CPU use. Those two numbers are **calibration inputs, not
tunables**. Measure them with a plug-in meter, once with the machine quiet and
once with every core busy. Left at `0`, the node reports `—` rather than a figure
nobody measured; that is deliberate, because an invented number is
indistinguishable from a measured one once it is on screen.

`smart_plug`, `ipmi` and `nvml` cost wall time in the maintenance loop — an HTTP
request and two subprocesses — so each takes `EXTERNAL_TIMEOUT_SECONDS`, and one
that is not configured is never asked.

Per-instance watts are the guest's share of the *host's* CPU over the interval,
read from its cgroup, so they do not move when another guest starts. Whatever no
instance accounts for — the host's own work, nodo itself, idle draw — is not
attributed to anybody. Delegated instances get nothing: their power is burnt on
the peer that runs them.

## `pricing`, `free_tier`, `ui`, `deposits`

What this node charges, in **MU** — its own unit of account. What an MU is worth is set
per payment system (`ledgers.ergo.payments.MU_PER_NANOERG`, below) and what you read is
set by `ui.DISPLAY_UNIT`. Full model and worked examples: [`PRICING.md`](PRICING.md).

| Key | Default | Meaning |
|---|---|---|
| `pricing.RAM_MU_PER_GIB_HOUR` | `1000000` | Memory held, per GiB-hour. |
| `pricing.CPU_MU_PER_VCPU_HOUR` | `4000000` | Compute held, per vCPU-hour. |
| `pricing.DISK_MU_PER_GIB_HOUR` | `100000` | Disk held, per GiB-hour. |
| `pricing.NET_MU_PER_GIB` | `2000000` | Tunnelled traffic, both directions (see [`TUNNELING.md`](TUNNELING.md)). |
| `pricing.BUILD_MU` | `10000000` | Building a service container, charged once. |
| `pricing.TUNNEL_OPEN_MU` | `10000` | Opening a tunnel, charged once. |
| `pricing.MODIFY_RESOURCES_MU` | `10000` | Changing a running instance's resources. |
| `pricing.SCARCITY_MAX_MULTIPLIER` | `10` | Ceiling of the surcharge when a resource runs out. `1` prices purely by consumption. |
| `pricing.SCARCITY_CURVE` | `1.0` | How fast the surcharge arrives. `1.0` is linear; higher stays near 1x until the resource is genuinely scarce. |
| `free_tier.CREDIT_MU_PER_NEW_CLIENT` | `4500000` | Starting balance given to every new client: one hour of 0.5 GiB of RAM plus one vCPU at the shipped prices. `0` gives nothing away, which with `costs.ALLOW_DEBT` off refuses every new client at its first launch. |
| `free_tier.FREE_WHILE_SCARCITY_BELOW` | `0.0` | Charge nothing while *every* resource is below this share of capacity. `0.0` disables it. |
| `ui.DISPLAY_UNIT` | `erg` | What you read and type. `erg`, `mu`, or a name declared under `ui.UNITS`. Purely presentational. |
| `deposits.AUTOMATIC_REFILL` | `true` | Whether the manager may pay a peer on its own. Set `false` and no tick ever broadcasts a refill: a peer's deposit runs down and stays down until you run `nodo pay` or `nodo increase_peer_deposit`. Delegation, peer refreshes and the cold-wallet sweep are unaffected — the sweep moves this node's funds between its own wallets and pays nobody. |
| `deposits.MAX_FEE_OVERHEAD` | `0.02` | Largest share of a peer deposit that may go to the transaction fee. Sizes the deposit. |
| `deposits.REFILL_BELOW` | `0.2` | Refill a peer once its balance drops below this share of a full deposit. |
| `deposits.INITIAL_RUNTIME_HOURS` | `1.0` | How long a new instance is funded for when the client asks for no specific balance. |

Prices are whole MU: there is nothing smaller to express, so a fractional one is refused
rather than rounded. Set any price to `0` to give that resource away.

A display unit other than `erg`/`mu` is declared explicitly — the hook for showing a
fiat figure later. Its rate is static and nothing refreshes it, so it goes stale; it
never affects what is charged:

```yaml
ui:
  DISPLAY_UNIT: usd
  UNITS:
    usd: { MU_PER_UNIT: 500000000, SYMBOL: "USD", DECIMALS: 2 }
```

## `balancers`

Every parameter of the peer-selection formula, and nothing else. `SOCIALIZATION_FACTOR`
and `COST_AVERAGE_VARIATION` moved here from `costs:`, unchanged — they were always peer
selection rather than pricing — and the rest of the formula now lives beside them.

A candidate is ranked by an effective cost in log space:

```
score(peer)  = −ln(cost_mu) + SOCIALIZATION_FACTOR · r̂ + ONCHAIN_REPUTATION_WEIGHT · ô
                            + DONATION_WEIGHT · d̂
score(local) = −ln(cost_mu) + LOCAL_BIAS              + DONATION_WEIGHT · d̂

r̂ = r / (|r| + REPUTATION_HALF_CREDIT)   ∈ (−1, 1)   sign-preserving: a peer that
                                                     failed us is still penalised
ô = S / (|S| + ONCHAIN_REPUTATION_HALF_CREDIT)  ∈ (−1, 1)
    S = Σ_p cred(p) · sign(v_p) · min(|v_p| · burned_erg(p), ONCHAIN_PUBLISHER_CAP)
    v_p     = the share of its own proof p stakes on this peer, netted
    cred(p) = max(0, cos(v_p, r̂))  — how far p's opinions about peers agree
                                      with ours, over the peers we both rate
d̂ = C / (C + DONATION_HALF_CREDIT)       ∈ [ 0, 1)   bonus only, never a penalty
```

`local` gets neither reputation term: this node holds no evidence about itself, and what
the chain says about it is what it published.

`ô` is the *on-chain* reputation — a different quantity that shares the name and, unlike
`r̂`, one that can be bought (an opinion is worth `share × burned ERG`). The burn **is**
counted, and `cred(p)` is what keeps counting it from being a way to buy a routing
decision: a reputation proof is not a peer, so instead of asking who owns it, this node
asks what it has said. Each proof's opinions about peers are compared with our own local
scores for those same peers — the cosine over the peers both of us rate — and its burn is
scaled by the result.

That is worth reading twice, because it is the whole design. A proof that praises a peer
that failed us is discredited **by that praise**, not by a rule about its owner. A proof
we share no ground with scores `0` and its burn buys nothing, which is the default and
the common case: a freshly minted proof that has only ever spoken about its own node
overlaps with us nowhere. And a newcomer can still earn a voice by agreeing with us about
peers we both know, which an owner test could never allow. Local reputation stays on
**peers only** — nothing is stored about proofs, `cred` is computed at read time.

`cred` is clamped at zero rather than sign-preserving: disagreement silences a proof, it
does not invert it. Were it to invert, paying a proof to denounce a rival would promote
that rival. It is also mirrorable — this node publishes its own scores
(`submit_to_ledger`), so agreement can be bought by copying them — which is why the cap,
the saturation and `ONCHAIN_REPUTATION_WEIGHT ≤ DONATION_WEIGHT` are what the safety
actually rests on: the most a perfect mirror buys is the term's ceiling, priced below 3
ERG donated. The chain is read on an hourly tick into SQLite; a routing decision does no
network I/O, and an index that has never filled scores **every** candidate zero, never
some.

Because price enters as a logarithm, **each weight is the maximum equivalent price
discount**: a weight of `W` lets the best possible candidate on that term beat a price up
to `e^W` higher, and never more. That is the whole reason for this shape — the previous
one divided reputation by the network's total, which made it worth ~0.2 against a
`log(cost)` of ~14, so price decided every comparison and reputation broke only near-exact
ties.

| Key | Default | Meaning |
|---|---|---|
| `balancers.SOCIALIZATION_FACTOR` | `2` | Weight of a peer's reputation, i.e. the largest price premium reliability can beat (`e²` ≈ 7.4×). |
| `balancers.REPUTATION_HALF_CREDIT` | `50` | Reputation at which half that weight is earned. A peer's standing no longer depends on how many peers exist. Also what bounds our own opinion vector when `cred(p)` is computed. |
| `balancers.ONCHAIN_REPUTATION_WEIGHT` | `0.1` | Weight of what the **ledgers** say about a peer (`e^0.1` ≈ 11 % premium at most). **Read `ONCHAIN_REPUTATION_WEIGHT / DONATION_WEIGHT` as the exchange rate between destroying one ERG and donating one** — at `0.1 / 0.3` a donated ERG is worth three burned ones, on purpose. The node **refuses** a config where this exceeds `DONATION_WEIGHT`: on-chain reputation is bought by burning, and weighing the burn higher makes donating the worse buy, so the money that funds this software gets destroyed instead. See [`DONATIONS.md`](DONATIONS.md) and issue #353. |
| `balancers.ONCHAIN_REPUTATION_HALF_CREDIT` | `20.0` | Agreed, burn-weighted ERG (`S`) at which half that weight is earned. `20` is four proofs at the cap, each in full agreement with us — a coalition, not a purchase. |
| `balancers.ONCHAIN_PUBLISHER_CAP` | `5.0` | The most any one proof may contribute to `S`, in ERG. Equal to `DONATION_HALF_CREDIT` on purpose: one proof, however funded and however agreeable, is worth at most what one median donation is. |
| `balancers.COST_AVERAGE_VARIATION` | `1` | How much a quote's variance inflates its cost when candidates are compared. |
| `balancers.DONATION_WEIGHT` | `0.3` | Weight of a peer's donation credit (`e^0.3` ≈ 35 % premium at most). **The safety parameter** — a high value closes the network to newcomers; see [`DONATIONS.md`](DONATIONS.md). |
| `balancers.DONATION_HALF_CREDIT` | `"5000000000"` | Donation credit, in MU, at which half that weight is earned. |
| `balancers.DONATION_AGE_SCALE` | `31536000` | One year, in seconds. An old donation weighs more: `1 + ln(1 + age / this)`. |
| `balancers.LOCAL_BIAS` | `1.0` | How much this node prefers running work itself. `1.0` reproduces the existing policy exactly (local tolerates a price up to `e¹` ≈ 2.7× higher than a peer's); it used to be a flat reputation of 1 hidden inside a branch. |

Weights must not be negative, and the half-credits must be positive — the node refuses
the config otherwise. A negative donation weight would turn the count list into a
punishment mechanism, which is what would make patching donations out rational. And
`ONCHAIN_REPUTATION_WEIGHT` must not exceed `DONATION_WEIGHT`, for the reason in that
row: it is the one comparison in this block that decides an incentive rather than a
ranking.

## `costs`, `timing`, `client`

What is left after pricing and peer selection moved out: `TUNNEL_CHARGE_INTERVAL_KB`
(how much traffic accumulates before it is billed) and `ALLOW_DEBT`; plus
maintenance-loop timing and client slot/expiration policy.

| Key | Default | Meaning |
|---|---|---|
| `client.ACCEPT_NEW_DEPOSITS` | `true` | Set `false` to stop `GenerateDepositToken` for every client (local or peer): no one can open a new deposit, so no one can acquire MU beyond what they already hold. Existing balances keep spending normally -- this only closes the door on new top-ups. Use to stop onboarding new demand, or to cap growth even while demand exists. |

## `host_limits` — how much of this machine nodo may take

For a node sharing a PC with the person using it. Nothing else in the config refuses a
paid workload: `pricing.SCARCITY_*` makes a loaded machine expensive and `low_demand`
gates only the opportunistic fallback, so without these a client may rent every core and
every byte the host has.

CPU, RAM and disk are **admission ceilings**. They are checked at launch and on every
resize, against the sum of what every instance has been *granted* (the `local_instances`
row the maintenance tick prices it by) plus what the newcomer asks for. That sum bounds
real usage rather than estimating it: the hypervisor holds each guest to the memory size,
CFS quota and image size it was created with. Nothing here samples live load.

Network has no grant to add up, so it is metered as it flows: the day's volume is counted
and the throughput shaped, both from the tunnel relay. Only **tunnelled** traffic
(see [`TUNNELING.md`](TUNNELING.md)) passes through there — an instance reachable on a
port of its own talks to the world without touching this node's relay.

The ceilings apply to every instance this node starts, its own included. A cap on what
nodo occupies that the operator's own instances could step over would not be a cap.

| Key | Default | Meaning |
|---|---|---|
| `host_limits.ENABLED` | `false` | Master switch. Off, none of the ceilings below apply. |
| `host_limits.MAX_CPU_SHARE` | `0.5` | Share of the host's **physical** cores that every instance's CFS quota may add up to. `0` lifts this ceiling. |
| `host_limits.MAX_RAM_SHARE` | `0.5` | Share of total memory that every instance's memory limit may add up to. |
| `host_limits.MAX_DISK_SHARE` | `0.5` | Share of the filesystem holding `main.STORAGE` that every instance's image may add up to. Measured against its **total** size, not its free space, so a disk something else filled does not quietly raise this node's allowance. |
| `host_limits.MAX_NET_GIB_PER_DAY` | `0` | Tunnelled traffic, both directions, per local calendar day. Spent, no tunnel opens and the open ones close; it resets at midnight and the running total survives a restart (table `tunnel_traffic`). `0` is unlimited. |
| `host_limits.MAX_NET_MIB_PER_SECOND` | `0` | Ceiling on tunnelled throughput across every tunnel at once. Shapes the relay by making it wait rather than closing anything, so a transfer over the ceiling gets slower and still finishes. `0` is unlimited. |

A refusal names the key that would change it, and every ceiling the instance breaches
rather than the first — an operator told only about memory would raise the memory share,
retry, and be told about disk. A capacity psutil cannot report lifts its own ceiling: an
unknown total is not evidence of a small one, and the memory pool and free-disk checks
still apply either way.

Edited from the TUI's Cell page, under `WALL · footprint & hours`.

## `activity_window` — the hours work is taken in

The other half of `host_limits`: that one bounds how much of the machine may be rented,
this one bounds when. Rent the PC out overnight; keep it to yourself while you are
working.

Outside the window the node refuses **new** work: a client's `StartService`, a peer's
`GetServiceEstimatedCost`, a peer's `GetResourceAvailability`, and a running instance
asking for a child. Work descended from a dev client is exempt, which is what keeps
`nodo execute`, the core services and `nodo pack` working at any hour — the window is
about renting this machine out after hours, not about locking its owner out of it.

| Key | Default | Meaning |
|---|---|---|
| `activity_window.ENABLED` | `false` | Master switch. |
| `activity_window.START` | `"00:00"` | Local time, `HH:MM`, inclusive. |
| `activity_window.END` | `"00:00"` | Local time, `HH:MM`, exclusive. Earlier than `START` wraps around midnight: `22:00`–`06:00` is one window, open all night. Equal to `START` means always open, so enabling the section before choosing the hours refuses nothing. |
| `activity_window.ON_CLOSE` | `refuse` | `refuse` stops taking new work and leaves running instances alone — they keep being charged, and an empty balance still reaps them. `stop` **also stops every instance not descended from a dev client** the moment the window closes, refunding what its balance still holds; its work is destroyed mid-flight, so only ask for it on a machine whose hours are genuinely not negotiable. |

A malformed `START` or `END` is rejected at load. A window that somehow reaches the
runtime unparseable leaves the node open and logs once: taking the node off the network
over a typo would be a silent outage where a log line is enough.

Edited from the TUI's Cell page, under `WALL · footprint & hours`.

## `identity` — the node's name

| Key | Default | Meaning |
|---|---|---|
| `identity.MNEMONIC` | `""` | The one mnemonic behind this node's `peer_id`. The Ed25519 key derived from it signs `GetPeerInfo` and backs the TLS certificate. Empty or `"auto"` generates a fresh one on first load — a node always needs a name. **Secret.** |

On no ledger, and deliberately separate from the wallets below: each wallet signs this
identity on each reputation proof it publishes, and that pair travels with it, so a wallet can
be added, dropped or rotated without every peer seeing a different node. Changing
`identity.MNEMONIC` *does* make a new node, orphaning the deposits and reputation
recorded against the old one. See [Node identity](CONCEPTS.md#node-identity).

## `ledgers.ergo` — payments & reputation

There is a **single** Ergo wallet. Clients pay its derived P2PK address; excess is
swept to a cold wallet once thresholds are met. Payments/reputation require Java
(see [`INSTALL.md`](INSTALL.md)).

| Key | Default | Meaning |
|---|---|---|
| `ledgers.ergo.WALLET_MNEMONIC` | `""` | The one wallet the node controls: what it is paid into, what publishes its reputation proofs, and what attests its identity on Ergo. Not the node's identity. Empty disables payments/reputation; `"auto"` generates a fresh mnemonic on first load. **Secret.** |
| `ledgers.ergo.NODE_URL` | `https://node.sigmaspace.io` | Ergo node used for chain access. |
| `ledgers.ergo.payments.MU_PER_NANOERG` | `1` | What one nanoERG buys in MU — the one place the node's unit of account meets real money, and what peers are told as `ContractRate.mu_per_unit`. ERG↔nanoERG is fixed in code, not here. |
| `ledgers.ergo.reputation.REPUTATION_PROOF_ID` | `""` | This node's reputation proof id (reconciled by `nodo sync_reputation_proof`). |
| `ledgers.ergo.payments.HOT_WALLET_LIMITS` | `100` | Max ERG kept in the operational wallet before sweeping. |
| `ledgers.ergo.payments.COLD_WALLET` | `""` | Public address to sweep excess to. Empty disables sweeping. Never a mnemonic. |
| `ledgers.ergo.payments.DONATION_PERCENTAGE` | `"0.02"` | Share of **incoming payments** donated to the people who write this software. Applies to earnings, not to the sweep below, and works with no cold wallet. The transaction fee comes out of this share, never on top of it. Set it to `0` to opt out. |
| `ledgers.ergo.payments.DONATION_WALLETS` | one address | Who this node funds: `{ address, weight }` entries. Weights are normalised and split what has accrued. A share below Ergo's minimum output stays accrued rather than being handed to the other wallets. |
| `ledgers.ergo.payments.DONATION_MIN_TRANSFER` | `"0.1"` | Smallest donation payout (ERG, decimal string). Below it the debt keeps accruing. |
| `ledgers.ergo.payments.DONATION_CREDIT_WALLETS` | one address | Whose contributions this node recognises when it routes work: `{ address, weight }` entries, normalised within the ledger. **A trust decision** — a bad pay list costs you, a bad count list is paid for by every peer you route to. |
| `ledgers.ergo.payments.DONATION_MIN_CONFIRMATIONS` | `10` | Confirmations a donation needs before it counts. The mempool is never read. |

Donations are read off the chain by every node independently and never announced over
the protocol; what a donor gets is a bounded bonus in *other* nodes' peer selection. The
full mechanism, the two lists and why they are separate: [`DONATIONS.md`](DONATIONS.md).
`nodo donations` prints what this node pays, what it counts, and what is accrued.

### `ledgers.bitcoin`

A second payment system, off until `payments.MU_PER_SATOSHI` is set — and that key has
no default on purpose: a satoshi is worth about a million nanoERG, so borrowing
`MU_PER_NANOERG`'s `1` would sell an hour of compute for a millionth of its price.
Unset, the node does not offer Bitcoin at all rather than offering it mispriced.

`BACKEND` decides what this node can do and where the key is. With `explorer` (a public
HTTP API) there is no key anywhere and the node can only be *paid*, at
`payments.COLD_WALLET`: with no key there is no hot wallet to be paid into and no sweep
to cold later, so the cold wallet is where payers are sent. With `core` the key
is in the wallet of a bitcoind you run and back up, and `WALLET_KEYS_EXTERNAL: true` is
what tells the node not to generate a mnemonic for it.

With `service` the node runs the bitcoind itself, as the `bitcoin-node` core service,
and derives its wallet from `WALLET_MNEMONIC` here — Ergo's posture, with Core still
doing the signing. That needs `WALLET_KEYS_EXTERNAL: false` (so the node mints the
mnemonic), `RPC_USER`/`RPC_PASSWORD` (Core's cookie lives inside the service and cannot
be read from here) and `core_services.bitcoin-node`. `PRUNE_MIB` sizes the chain it
keeps: `0` is the whole ~700 GB with a `txindex`, anything else prunes to about that many
MiB. All of it is checked at startup.

Every key, what it does, the BIP-84 path the service derives at, and why on-chain BTC is
for coarse node-to-node deposits rather than for a client topping up an instance:
[`BITCOIN.md`](BITCOIN.md).

> ⚠️ `WALLET_MNEMONIC` is a secret — on either ledger. With Bitcoin's `service`
> backend this file is the *only* backup of that wallet: the service derives its keys
> and stores none. The `nodo tui` Config editor masks secret values; keep backups
> off-repo. Ergo and Bitcoin transactions are both **final and irreversible**
> (see [`KyA.md`](KyA.md)).

## `general_flags`, `misc`, `logs`, `low_demand`, `publisher`

- `general_flags.SIMULATE_PAYMENTS` — dry-run payments (dev).
- `misc.VALIDATE_ON_IMPORT` (`true`), `misc.CONFIGURATION_REQUIRED`.
- `logs.DEBUG_MODE`, `logs.MEMORY_LOGS`.
- `logs.TUNNEL_LOGS` — log every tunnel handshake, relay close and billing tick
  under `[TUNNEL]`. Off by default: one line per connection and per billed MiB
  buries the rest of the node log on a busy tunnel. See [`TUNNELING.md`](TUNNELING.md).
- `low_demand.*` — opportunistic idle scheduler (off by default; WIP).
- `publisher.*` — how `nodo publish` uploads a service and how a freshly-published
  source gets registered (GitHub repo, chunking, auto-publish-tx settings).
