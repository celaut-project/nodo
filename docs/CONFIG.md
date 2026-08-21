# Configuration Reference (`config.yaml`)

Nodo reads a single `config.yaml`, created from
[`config.example.yaml`](../config.example.yaml) at install time. It lives in the
installation root (`TARGET_DIR`, default `/nodo`), i.e. `/nodo/config.yaml`. The
`main.MAIN_DIR` value inside it is the same root. Edit it directly, or use the
`nodo tui` Config page, which edits in place with `yq -i` (preserving comments) and
snapshots the previous file to `config-<YYYYMMDDHHMMSS>.yaml` beside it.

> ⚠️ nodo **rewrites** `config.yaml` on its first load: `auto` values such as
> `network.GATEWAY_PORT` and `ledgers.ergo.WALLET_MNEMONIC` are resolved to
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

Cloud Hypervisor (`ch`) is the only virtualizer; the Docker virtualizer was
removed, so the node needs no local Docker install to *run* services.

| Key | Default | Meaning |
|---|---|---|
| `virtualizers.DEFAULT_VIRTUALIZER` | `ch` | Only `ch` is supported. |
| `virtualizers.ch.BINARY_PATH` | (set at install) | Cloud Hypervisor binary. |
| `virtualizers.ch.KERNEL_PATHS` / `INITRAMFS_PATHS` | per-arch | Guest kernel/initramfs per `linux/amd64` \| `linux/arm64`. The kernel is downloaded at install time from the `guest-kernel-vN` release (pinned as `GUEST_KERNEL_VERSION` in `install.sh`), not taken from the host's `/boot`; the initramfs is built locally by `bash/build_ch_initramfs.sh`. |
| `virtualizers.ch.NETWORK_MODE` | `tap_bridge` | Guest networking mode. |
| `virtualizers.ch.MIN_MEM_MIB` / `DEFAULT_MEM_MIB` | `128` / `256` | Boot memory floor / default. |
| `virtualizers.ch.SECURITY.*` | — | rootfs path confinement, device-node policy, trusted-service allowlists. |

## `builder` — architectures the node can *execute*

These keys govern **which architectures the node can EXECUTE** — they drive
`SUPPORTED_ARCHITECTURES` (`src/utils/architectures.py`). Setting **both** to
`false` means the node executes nothing. Do **not** confuse them with the
packer-side pair below (`packer.ARM_PACKER_SUPPORT` / `X86_PACKER_SUPPORT`), which
only affect what `nodo pack` builds/announces: to limit *execution* architectures,
edit these `builder.*` keys, not the packer pair.

| Key | Default | Meaning |
|---|---|---|
| `builder.ARM_SUPPORT` | `true` | Node can execute `linux/arm64` services. |
| `builder.X86_SUPPORT` | `true` | Node can execute `linux/amd64` services. |
| `builder.WAIT_FOR_UNLOCK_MEMORY` | `60` | Seconds to wait for a memory lock to release during a build (`src/utils/utils.py`). |

## `communication` — peer messaging policy

`communication.*` tunes peer-to-peer messaging behaviour —
`SELF_ANNOUNCE_TO_CONNECTING_PEERS`, `SEND_ONLY_HASHES_ASKING_COST`, and
`DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH` (read by `src/commands/connect.py` and
the execution balancer).

## `packer` — how `nodo pack` builds

The most important choice for anyone packing services. Full authoring format:
[`PACKING.md`](PACKING.md).

| Key | Default | Meaning |
|---|---|---|
| `packer.local` | `false` | `false` → delegate the build to a **packer-service** microVM (no builder on this host). `true` → build **locally** with nodo's rootless BuildKit toolchain (provisioned on demand, no sudo). |
| `packer.PACKER_SOURCE_URL` | `""` | Manifest URL nodo downloads the packer service from directly when it needs to acquire it. Empty → resolve via the `source-application` core service. |
| `packer.PACKER_SERVICE_URL` | `""` | Override: `ip:port` base URL of an out-of-band packer-service. Used only when no packer id is set / no running instance is found. |
| `packer.ARM_PACKER_SUPPORT` / `X86_PACKER_SUPPORT` | `true` | Architectures `nodo pack` accepts/announces (**packer-side** — to limit what the node can *execute*, use `builder.*` instead). |
| `packer.PACKER_MEMORY_SIZE_FACTOR` | `2.0` | Local-packer only: RAM to lock as a factor of the exported filesystem size. |
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
| `hashing.HASH` | `sha3_256` | Service/file identification hash. Accepts `sha3_256`, `sha256`, `shake_256`, `blake2b`, or a hex hash-id. |
| `hashing.CHECK_INTEGRITY_ON_SERVE` | `false` | Run integrity/migration automatically on `nodo serve`. |

## `network`

Controls exposure and remote execution. Key entries: `GATEWAY_PORT` (`auto`),
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

## `ledgers.ergo` — payments & reputation

There is a **single** Ergo wallet. Clients pay its derived P2PK address; excess is
swept to a cold wallet once thresholds are met. Payments/reputation require Java
(see [`INSTALL.md`](INSTALL.md)).

| Key | Default | Meaning |
|---|---|---|
| `ledgers.ergo.WALLET_MNEMONIC` | `""` | The one wallet the node controls. Empty disables payments/reputation; `"auto"` generates a fresh mnemonic on first load. **Secret.** |
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
- `misc.MIN_BUFFER_BLOCK_SIZE` (`1.0e+7` = 10 MB) — inline/block threshold: files
  at or above this size are stored as content-addressed blocks (`main.BLOCKDIR`),
  smaller ones inline.
- `misc.VALIDATE_ON_IMPORT` (`true`), `misc.CONFIGURATION_REQUIRED`.
- `logs.DEBUG_MODE`, `logs.MEMORY_LOGS`.
- `logs.TUNNEL_LOGS` — log every tunnel handshake, relay close and billing tick
  under `[TUNNEL]`. Off by default: one line per connection and per billed MiB
  buries the rest of the node log on a busy tunnel. See [`TUNNELING.md`](TUNNELING.md).
- `low_demand.*` — opportunistic idle scheduler (off by default; WIP).
- `publisher.*` — how `nodo publish` uploads a service and how a freshly-published
  source gets registered (GitHub repo, chunking, auto-publish-tx settings).
