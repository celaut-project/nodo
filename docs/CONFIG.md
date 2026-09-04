# Configuration Reference (`config.yaml`)

Nodo reads a single `config.yaml`, created from
[`config.example.yaml`](../config.example.yaml) at install time. It lives in the
installation root (`TARGET_DIR`, default `/nodo`), i.e. `/nodo/config.yaml`. The
`main.MAIN_DIR` value inside it is the same root. Edit it directly, or use the
`nodo tui` Config page, which edits in place with `yq -i` (preserving comments) and
snapshots the previous file to `config-<YYYYMMDDHHMMSS>.yaml` beside it.

A change made from the TUI is applied as a transaction: back up, write, restart nodo,
and restore the backup if the node does not come back up on it. So the file always
describes the running node — an edit is never left waiting for a restart. The Cell
page is the same mechanism at a coarser grain: it groups these keys into the decisions
an operator actually makes, and one of its levers or profiles writes several keys as a
single change. See [the TUI reference](../src/commands/tui/README.md#applying-a-change).

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
| `communication.SHARE_PROSE_ON_GET_PEER_INFO` | `true` | Prose in what `GetPeerInfo` serves. On: the bytes are transient, and a peer that cannot read what this node means by its tags is exactly the reader it is written for. |
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
disables it, see [`CONCEPTS.md`](CONCEPTS.md)),
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
| `free_tier.CREDIT_MU_PER_NEW_CLIENT` | `0` | Starting balance given to every new client. |
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

## `costs`, `timing`, `client`

What is left after pricing moved out: `SOCIALIZATION_FACTOR` and
`COST_AVERAGE_VARIATION` (peer selection, not pricing), `TUNNEL_CHARGE_INTERVAL_KB`
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
| `ledgers.ergo.payments.DONATION_WALLET` / `DONATION_PERCENTAGE` | addr / `0.00` | Optional donation of a share of earnings. |

> ⚠️ `WALLET_MNEMONIC` is a secret. The `nodo tui` Config editor masks secret
> values; keep backups off-repo. Ergo transactions are **final and irreversible**
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
