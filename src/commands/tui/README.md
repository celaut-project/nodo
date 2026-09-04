# Nodo operations console

`nodo tui` opens a terminal operations console for a running nodo installation. It reads the
same `config.yaml`, SQLite database, registries, Cloud Hypervisor cgroups, logs, and wallet
status used by the node. Paths are resolved from `config.yaml`; they are not hard-coded to a
particular installation directory.

## Pages

| Page | Purpose |
|---|---|
| **Overview** | Node status/version/address, host CPU and RAM, current and reserved instance resources, disk usage, nodo storage size, peer/client counts, service count, reputation proof, and Ergo wallet balances. |
| **Instances** | Running instances with service, endpoint, virtualizer, balance, and what each one is *using* rather than only what it was allocated: live CPU%, memory used against its limit, and network rates. The detail card adds the vCPU allowance the CPU% is measured against, cumulative disk and network totals, and the disk allocation. |
| **Services** | Locally available services, metadata tag, content ID, stored size, and execution action. The detail card carries the service's reputation — accumulated over every instance of it that has run here, since an instance is gone minutes after it misbehaves — and the events behind it. |
| **Peers** | Who we talk to: endpoints, our balance with each, reputation, and the payment contracts and rates a peer declares. The detail card adds every payment we have made to the selected peer — including one broadcast that the peer never acknowledged — and the reputation events behind its score, each with the reason that produced it. Peers can be connected (`c`) and forgotten (`d`) from here. |
| **Clients** | Who pays us: balance, last usage, and whether the client is metered at all. The detail card lists what it has paid, the deposit tokens it holds and what became of them, and the instances it started here. A client cannot be resolved to a peer and the page does not pretend otherwise (see issue #178). Balance can be credited/debited with `+`/`-`. |
| **Cell** | The node's policies as a set of named decisions, laid out as a cell: what it lets in, what work it takes, what it says to the network, what it distrusts, how it charges, and what it keeps. One row is one decision, and moving it writes every key that decision spans. Postures ("just me", "cautious renter", …) apply a whole set at once, and the page says which one this node is closest to. |
| **Pricing** | What this node charges, per resource, as vertical bars you can nudge. Recurring and one-off prices are charted apart because their magnitudes are unrelated. Beside them: the display unit, what one MU is worth on the ledger, the scarcity ceiling, and a worked hourly example. |
| **Config** | Every scalar or empty collection in `config.yaml`, including values inside lists. Values retain their YAML type when edited, and list elements can be added and removed. |
| **Logs** | Tail of `storage/app.log` beside commands/actions launched from the TUI. |

## Money

Amounts are stored in **MU**, the node's unit of account, and rendered in whatever
`ui.DISPLAY_UNIT` says (ERG by default). The TUI reads the catalogue database directly, so it
resolves the display unit, the ledger rate (`ledgers.ergo.payments.MU_PER_NANOERG`) and the
price vector from `config.yaml` itself — the same three settings the node uses, documented in
[`docs/PRICING.md`](../../../docs/PRICING.md). Formatting happens at draw time rather than at
read time, so changing the unit takes effect on the next frame.

The Ergo wallet card on Overview is the exception: it shows on-chain ERG from `nodo info`, not
a node balance, and is never converted.

Prices written from the Pricing page go through the same transaction as every other
configuration change — see [Applying a change](#applying-a-change).

Ergo information is refreshed asynchronously through `nodo info` every 60 seconds so JVM or
explorer latency cannot freeze the interface. Local database/system data refreshes every two
seconds; the recursive storage scan is limited to every 30 seconds.

## Live instance usage

The Instances page reads each instance's usage from the same places `nodo observe` does, on
every two-second sweep: `cpu.stat`, `memory.current` and `io.stat` inside the instance's
cgroup (`<virtualizers.ch.CGROUPS_BASE_DIR>/nodo-ch/<id>`), and the byte counters of its tap
interface under `/sys/class/net`. The tap name is re-derived from the instance id rather than
stored, so it cannot drift from the one the virtualizer programmed. CPU% and the network rates
are deltas between consecutive sweeps, which is why they read `—` for one tick after an
instance appears.

CPU% follows `nodo observe`'s convention: **cumulative core time, not normalised by the vCPU
count**, so an instance saturating two vCPUs reads `200%`. The detail card states the
allowance it is measured against — taken from the cgroup's `cpu.max`, the ceiling actually
being enforced, because the `cpu_period`/`cpu_quota` columns on `local_instances` are stored
as `0` — and the CPU cell turns amber once the instance is within a tenth of that allowance.

A figure reads `—` when it cannot be measured, never `0`: an idle instance and one we cannot
see into are different claims. Expect `—` for every live figure on a delegated instance (it
runs on another peer, so there is no local cgroup or tap), and for the disk read/write totals
whenever the `io` controller is not delegated to the instance's leaf cgroup, which is the
common case.

## Controls

| Key | Action |
|---|---|
| `Tab` / `Shift+Tab` | Next/previous page (both wrap) |
| `↑` / `↓` | Select table row, or move through the Config tree |
| `→` / `←` | Enter/leave a Config branch (see below), move between Cell organelles; ignored by the other pages |
| `r` | Force a refresh |
| `c` | Connect a peer, from Peers |
| `a` | Config: append an element to the selected list |
| `d` | Delete the selected service, forget the selected peer on Peers, remove the selected Config list element, or show how this node deviates from its closest profile on Cell |
| `k` | Kill the selected instance |
| `g` | Instances: dependency tree / flat list |
| `i` | Service details |
| `e` | Execute the selected service, or edit the selected Config, Pricing or Cell value |
| `p` | Cell: apply a profile |
| `+` / `-` | Adjust peer reputation on Peers, the selected price by 10 % on Pricing, or open a credit/debit amount modal on Clients |
| `n` | Cell: the router steps (`nodo nat-guide`) |
| `/` | Filter Config paths/values |
| `x` | Clear the Config filter |
| `Enter` / `Space` | Expand/collapse the selected Config section, or move the selected Cell lever to its next position |
| `Enter` / `Esc` | Save/cancel a modal |
| `Ctrl+U` | Clear modal input |
| `q` or `Ctrl+C` | Exit |

`d` on Peers runs `nodo disconnect <peer id>`, which drops the peer row together with
its addresses and contract instances. The peer is **forgotten, not banned**: it can
re-introduce itself, or be reconnected with `c`. Use it on a peer whose addresses went
stale — notably one that reinstalled and came back under a new identity key, since
`nodo connect` moves the address to the new peer_id and leaves the old row behind.

`+`/`-` on Clients opens an amount modal (typed in `ui.DISPLAY_UNIT`, same as the balance
column) and, on `Enter`, runs `nodo credit_client <client id> <amount>` or
`nodo debit_client <client id> <amount>` in the background.

## Applying a change

Every configuration change made in this TUI — a raw key on Config, a price on Pricing,
a lever or a profile on Cell — is applied as one transaction:

1. `config.yaml` is snapshotted to `config-<YYYYMMDDHHMMSS>.yaml` beside it (the ten
   most recent are kept, matching what the Python `ConfigManager` prunes to).
2. The change is written with nodo's configured `yq`, in place, comments preserved.
   A change that spans several keys — a lever, a profile — is **one** `yq` invocation,
   so the file never holds half of it.
3. If something is serving on the gateway port, `nodo daemon restart` runs and the
   port is waited on until it answers again.
4. **If the node does not come back, the snapshot is put straight back** and the node
   is restarted on it.

So what the file says is what the running node loaded. A change that cannot be
restarted into is not left on disk to be discovered later, and there is no state in
which the node's behaviour and its configuration disagree.

Two consequences worth knowing:

- **The restart needs root**, because `nodo.service` is a system unit
  (`nodo daemon restart` → `systemctl`). Run the TUI as root to edit configuration on
  a serving node; without it the restart fails, and the change is reverted rather
  than half-applied.
- **A node that is not serving is edited without a restart** — there is no running
  node to disagree with the file, so the change simply stands and the next start
  reads it. The status line says which of the two happened.

Values are handed to `yq` through the environment, never interpolated into the
expression, so nothing typed here can be read as yq syntax. `env()` rather than
`strenv()` means a value keeps its YAML type: `true` stays a bool, `2.0` a float,
`["*"]` a list.

## Cell

The Config page is the whole YAML tree, ordered by where a key lives in the file.
That is what you want when you already know the key. The Cell page is the other half:
a closed catalogue of *decisions*, each named by the question it answers.

The layout is a cell because the anatomy carries the grouping — the part of a cell
responsible for something is the part of the config responsible for it too:

| Organelle | What it decides |
|---|---|
| `CHANNELS · reach` | The gateway port, whether this node publishes its address, DDNS, whether an instance gets a port of its own |
| `RIBOSOMES · work` | Whether outside work is taken at all, foreign architectures, descendant admission, spare-capacity work |
| `VESICLES · voice` | Delegating work to peers and paying for it, announcing to peers, how much an announcement carries |
| `NUCLEUS · identity & wallet` | The identity mnemonic, the Ergo wallet, the cold wallet, and whether payments are real |
| `IMMUNE · trust` | Service egress, child isolation, integrity checks, device nodes, manifest claims |
| `MITOCHONDRIA · money` | The scarcity surcharge, the free tier, instance debt, the display unit — and a link to Pricing, which owns the prices themselves |
| `VACUOLE · upkeep` | Debug logging, failure retention, downloaded files |

A wide terminal draws all seven; a narrow one collapses to one column with the
focused organelle open. The keys are the same either way.

### Levers

One row is one decision, and it may span several keys. `debug mode` writes five of
them (`logs.DEBUG_MODE`, `logs.MEMORY_LOGS`, `logs.TUNNEL_LOGS`,
`virtualizers.ch.SERIAL_MODE`, `CONSERVE_RUNTIME_DIR_ON_FAILURE`); `delegate work`
writes two, because delegating and paying for it are separately answerable.

- `Enter` moves the lever to its next position — after showing every key that would
  change, and only writing on `y`.
- `e` opens the ordinary value editor on a single-key lever, or lists the keys behind
  a multi-key one so you can see exactly what one named position stands for. The
  Config page remains the place to break them apart.
- A row marked `⁓ custom` means the keys are set to a combination the catalogue has
  no name for. That is reported rather than rounded to the nearest position: the page
  will not misdescribe what your node is doing. `e` shows the keys; `Enter` moves it
  to the first named position.
- A `→` row is not a setting. It navigates to the page that owns it.

### Profiles

`p` applies a posture — a whole set of policy keys at once, ordered from the most
closed to the most open:

| Profile | For |
|---|---|
| `JUST ME` | I run my own things here. Nothing from outside, nothing spent outside. |
| `CAUTIOUS RENTER` | I will rent this machine out, but on a short leash. |
| `OPEN RENTER` | I want this machine earning: reachable, delegating, priced by load. |
| `LAN LAB` | A few machines on my own network, sharing capacity for free. |
| `WORKBENCH` | I am developing against this node. Nothing here is real money. |

A profile writes **policy only**. None of them touches an identity, a wallet, a
filesystem path, a port or a core-service id — a posture is a decision about how the
node behaves, never about who it is or where its things live.

Nothing records which profile is active. The page **derives** it by asking which
posture the file already satisfies, so it cannot go stale when a key is nudged
elsewhere, and `d` lists exactly where this node differs from the closest one. That
deviation report is the most instructive thing on the page: it is how an operator who
has never opened `config.yaml` learns their own configuration.

The catalogue is checked against the real `config.example.yaml` in the test suite, so
a lever cannot point at a key the node no longer reads, no two levers can own the
same key, and no profile can leave a lever in a state the page cannot name.

## Configuration editor

The Config page operates on the full YAML tree instead of a small hard-coded allowlist. For
example, list values appear as `core_services[1].id` and nested values as
`virtualizers.ch.MIN_MEM_MIB`. It is the node's only configuration editor — the
`nodo config` wizard it replaced has been removed ([`docs/CONFIG.md`](../../../docs/CONFIG.md)).

- `→` enters the selected branch, `←` collapses it or steps out to its parent, so a
  nested key is reachable (and escapable) with the arrows alone. `↑`/`↓` move through
  whatever is currently visible.
- Input is parsed as YAML, so `true`, `5000`, `1.5`, `null`, `[]`, and quoted strings retain
  the expected type.
- Lists are the one shape a single-value editor cannot cover, so they have their own two
  keys: `a` appends an element, `d` removes the selected one (asking first). `a` works on
  the list itself — an empty one is a leaf, which is the only way to fill it — and on any
  of its elements, which is where the cursor lands after an add. `d` only ever takes an
  element (`[0]`, `[1]`, …): with a key *inside* an element selected it says so rather
  than removing the element that key belongs to.
- A new element is a YAML literal like any other value, so a leading `*` has to be
  quoted (`"*.example.com"`) — there it is YAML's alias indicator, not text. A `*`
  anywhere else, as in `dns:*`, needs no quoting.
- The update is performed with nodo's configured `yq` binary, preserving comments and the
  rest of the file layout.
- Before every write, the previous file is snapshotted to `config-<YYYYMMDDHHMMSS>.yaml`
  beside it; the ten most recent are kept.
- Paths containing `mnemonic`, `password`, `secret`, `private_key`, `token`, or `api_key` are
  masked in tables and modal input. Leaving a secret editor blank keeps the existing value;
  enter `""` explicitly to clear it.
- A saved value is immediately visible in the TUI, but a running nodo process may require a
  restart before it observes the change.

## Development

The protobuf compiler is vendored through `protoc-bin-vendored`; no system `protoc` is needed.

```bash
cd src/commands/tui
cargo test
cargo clippy --all-targets -- -D warnings
cargo run
```

Render tests cover every page at 80×24 and 140×40, and a dedicated regression test verifies
that plaintext secrets never appear in the terminal buffer.
