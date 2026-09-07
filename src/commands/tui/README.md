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
| **Earnings** | What this node earned by being up, in both currencies it earns in, each drawn as the kind of quantity it is: money per payment network over the last day/week/month/year, because money is a flow; and what the network stakes on this node as a standing, with the ERG sunk behind it, because a chain that re-dates an opinion whenever its proof republishes cannot say when reputation was earned. Underneath, every proof that has staked something on this node. |
| **Cell** | The node's policies as a set of named decisions, laid out as a cell: what it lets in, what work it takes, what it says to the network, what it distrusts, how it charges, and what it keeps. One row is one decision, and moving it writes every key that decision spans. Postures ("just me", "cautious renter", …) apply a whole set at once, and the page says which one this node is closest to. |
| **Pricing** | What this node charges, per resource, as vertical bars you can nudge. Recurring and one-off prices are charted apart because their magnitudes are unrelated. Beside them: the display unit, what one MU is worth on the ledger, the scarcity ceiling, and a worked hourly example. |
| **Schedule** | The hours this node takes work in (`activity_window`), drawn as the day it is: the open stretch as one run of blocks, a marker at the current hour, and what closing time does to work already running. Underneath, on the same axis, a month of demand folded onto the 24 hours of a clock — peak instances held, and the work refused because the window was shut. Edited by moving an edge rather than by typing a time, so an unusable hour cannot be expressed. |
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

## Earnings

Two halves, read from different places, refreshed at different speeds, and drawn as
different kinds of quantity — which is the point of the page.

**Money is a flow**, so it is windowed: what came in over the last day, week, month,
year and all time, per payment network. It comes from the `payments` table on the
ordinary two-second sweep. Only `direction = 'in'` and `status = 'accepted'` counts as
earned — a `rejected` row is a deposit this node could not validate, so no balance was
credited for it and nothing arrived. Refused deposits are named in the block title
instead of being folded into a total, and money paid *out* is not earnings at all. One
row per ledger tag rather than one total: a node paid over two networks holds two
balances in two places, and summing them would name a figure the operator cannot spend.
A payment whose row carries no ledger tag is still money, and appears as `unknown`. `0`
is a window the catalogue was read for and nothing arrived in — a measurement, not a gap.

**Reputation is a stock**, so it is not windowed at all, and the card says so rather
than leaving a gap where the money's windows are. The chain cannot date what it holds:
revising an opinion spends its box and writes a new one, and a nodo proof re-splits its
whole supply across its peers on **every** submission — one peer reaching
`LEDGER_REPUTATION_SUBMISSION_THRESHOLD` events is enough — so every date on that proof
resets together. A "reputation earned this week" column would therefore report how often
the other node republishes, sitting next to real money flows and reading like one. Each
opinion still shows the age of its own box in the table below, which is all that date
honestly supports.

What is reported instead is the standing and what backs it, from `nodo reputation
--json`, re-read every five minutes (and on `r`). It is a subprocess rather than a query
because the lookup goes to the Ergo explorer and a frame must never wait on the network;
it is *that* command rather than a second implementation because the arithmetic has to
agree with what every other reader of the same chain computes. See
[Reading the reputation held on a node](../../../docs/ERGO.md#reading-the-reputation-held-on-a-node).

A reputation figure is a **share of what the staking proof has assigned to opinions**,
not a token count and not a share of everything it minted. A proof parks the supply it
has not assigned in a box pointing at itself, and on mainnet that reserve is nearly all
of it — every live profile holds ~99,999,9xx of its 99,999,999 tokens there and spends
one token per opinion. Against the minted supply every real opinion in the system reads
0.000001%, the same figure for all of them; against the assigned supply, one token out
of the ninety-five a proof has deployed reads 1.05%. Raw token counts are not shown at
all: what "1 token" is worth depends entirely on how many that proof has assigned, so
the share is the only figure that means anything on its own (`nodo reputation` prints
the counts for anyone who wants to check the arithmetic).

Beside it, `Backed by` says what that share cost: the ERG burned into the publishing
proof, which the reputation contract makes unrecoverable even by its owner, apportioned
by the share committed here. Read together, because minting a proof is free — 100 % of a
proof sitting at the min-box value it needs to exist is backed by 0.001 ERG, and the
page has to be able to tell that from a proof somebody sacrificed 10 ERG into. It is
shown in ERG and never through `ui.DISPLAY_UNIT`: MU is what *this* node charges in, and
somebody else's sunk cost is not a balance of ours to denominate (the same line the
Overview wallet card draws).

What is staked for and what is staked against are separate rows, because a stake against
is not a smaller stake for. `—` means the chain has not been read; a failed read leaves
the previous figures in place with the reason beside them, rather than blanking the page.

This node's own proof is shown but excluded from the totals. A node vouching for itself
is not reputation, and an operator holding a proof that stakes everything on itself
would otherwise wonder where that stake went.

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
| `↑` / `↓` | Select table row, move through the Config tree, or pick which edge of the working day the arrows move on Schedule |
| `→` / `←` | Enter/leave a Config branch (see below), move between Cell organelles, move the selected edge of the working day by 30 min on Schedule; ignored by the other pages |
| `r` | Force a refresh (on Earnings, re-reads the chain as well) |
| `c` | Connect a peer, from Peers; on Schedule, what closing time does (refuse / stop) |
| `a` | Config: append an element to the selected list |
| `d` | Delete the selected service, forget the selected peer on Peers, remove the selected Config list element, or show how this node deviates from its closest profile on Cell |
| `k` | Kill the selected instance |
| `g` | Instances: dependency tree / flat list |
| `i` | Service details |
| `e` | Execute the selected service, or edit the selected Config, Pricing or Cell value |
| `p` | Cell: apply a profile |
| `+` / `-` | Adjust peer reputation on Peers, the selected price by 10 % on Pricing, or open a credit/debit amount modal on Clients |
| `n` | Cell: the router steps (`nodo nat-guide`) |
| `w` | Schedule: enforce the hours, or stop enforcing them |
| `/` | Filter Config paths/values |
| `x` | Clear the Config filter |
| `Enter` / `Space` | Expand/collapse the selected Config section, move the selected Cell lever to its next position, or apply the edited working day on Schedule |
| `Enter` / `Esc` | Save/cancel a modal. On Schedule, `Esc` gives up an unapplied edit before it gives up the interface: a second `Esc` still quits |
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
a lever or a profile on Cell, the working day on Schedule — is applied as one
transaction:

1. `config.yaml` is snapshotted to `config-<YYYYMMDDHHMMSS>-<nnnn>.yaml` beside it (the ten
   most recent are kept, matching what the Python `ConfigManager` prunes to).
2. The change is written with nodo's configured `yq`, in place, comments preserved.
   A change that spans several keys — a lever, a profile, the four keys a working day
   is — is **one** `yq` invocation, so the file never holds half of it. That is also
   why Schedule collects an edit and applies it on `Enter` rather than writing per
   keypress: `START` and `END` are one decision, and a node restarted between them
   would be running a window nobody chose.
3. If something is serving on the gateway port, `nodo daemon restart` runs and the
   port is waited on until it answers again.
4. **If the node does not come back, the snapshot is put straight back** and the node
   is restarted on it.

So what the file says is what the running node loaded. A change that cannot be
restarted into is not left on disk to be discovered later, and there is no state in
which the node's behaviour and its configuration disagree.

Step 3 is not a convenience. `ConfigManager` reads `config.yaml` once per process and
never re-reads it, so a change that is not restarted into is a change the node never
sees — which is what makes this TUI the supported way to edit a serving node.

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
| `WALL · footprint & hours` | How much of this machine may be held at once (CPU, RAM, disk, network), and the hours of the day work is taken in |
| `MITOCHONDRIA · money` | The scarcity surcharge, the free tier, instance debt, the display unit — and a link to Pricing, which owns the prices themselves |
| `VACUOLE · upkeep` | Debug logging, failure retention, downloaded files |

A wide terminal draws all eight; a narrow one collapses to one column with the
focused organelle open. The keys are the same either way. A box shorter than its own
list of levers scrolls to whatever is selected, so nothing in it becomes unreachable.

### Levers

One row is one decision, and it may span several keys. `debug mode` writes four of
them (`logs.DEBUG_MODE`, `logs.MEMORY_LOGS`, `logs.TUNNEL_LOGS`,
`CONSERVE_RUNTIME_DIR_ON_FAILURE`); `delegate work` writes two, because delegating
and paying for it are separately answerable.

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
- Before every write, the previous file is snapshotted to
  `config-<YYYYMMDDHHMMSS>-<nnnn>.yaml` beside it; the ten most recent are kept. The
  stamp is UTC and the four trailing digits are random, so writes inside one second
  each keep a snapshot.
- Paths containing `mnemonic`, `password`, `secret`, `private_key`, `token`, or `api_key` are
  masked in tables and modal input. Leaving a secret editor blank keeps the existing value;
  enter `""` explicitly to clear it.
- A saved value is immediately visible in the TUI; a running nodo process observes it
  only after the restart above, because it reads `config.yaml` once at start.

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
