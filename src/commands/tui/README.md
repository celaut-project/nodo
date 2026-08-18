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
| **Pricing** | What this node charges, per resource, as vertical bars you can nudge. Recurring and one-off prices are charted apart because their magnitudes are unrelated. Beside them: the display unit, what one MU is worth on the ledger, the scarcity ceiling, and a worked hourly example. |
| **Config** | Every scalar or empty collection in `config.yaml`, including values inside lists. Values retain their YAML type when edited. |
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

Prices written from the Pricing page go through the same `yq` path as the configuration
editor, backup included. `nodo` must be restarted for a price change to affect a running node.

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
| `→` / `←` | Enter/leave a Config branch (see below); ignored by the other pages |
| `r` | Force a refresh |
| `c` | Connect a peer, from Peers |
| `d` | Delete the selected service, or forget the selected peer on Peers |
| `k` | Kill the selected instance |
| `g` | Instances: dependency tree / flat list |
| `i` | Service details |
| `e` | Execute the selected service, or edit the selected Config or Pricing value |
| `+` / `-` | Adjust peer reputation on Peers, the selected price by 10 % on Pricing, or open a credit/debit amount modal on Clients |
| `/` | Filter Config paths/values |
| `x` | Clear the Config filter |
| `Enter` / `Space` | Expand/collapse the selected Config section |
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
