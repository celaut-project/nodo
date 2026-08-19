# Pricing and the monetary unit (MU)

Status: **implemented.** This document replaces the gas model that used to be described
in `docs/CONCEPTS.md` and configured under the `costs:` block. "What is wrong today"
below is kept as the rationale: it describes the model this one replaced.

## What the gas model got wrong

The node kept two numeric worlds that never met, roughly 56 orders of magnitude
apart:

| World | Constants | Order |
| --- | --- | --- |
| Cost — what is actually charged | `EXECUTION_COST` 100, `BUILD_COST` 10, `TUNNEL_OPEN_COST` 10 | 1e0 – 1e2 |
| Money — deposits and balances | `MIN_DEPOSIT_PEER` 1e32, `TOTAL_REFILLED_DEPOSIT` 1e64, `DEV_CLIENT_GAS_AMOUNT` 1e256, `GAS_PER_ERG` 1e58 | 1e32 – 1e256 |

Measured consequences with the shipped defaults:

* A maintenance tick costs at most 100 gas per 10 s, which converts to **0 nanoERG**.
  Every real charge is worth exactly nothing.
* 1e49 gas is needed to be worth a single nanoERG; 1e55 to reach Ergo's minimum box
  value. No charge the node ever computes can be settled on-chain.
* `TOTAL_REFILLED_DEPOSIT` (1e64) converts to 1e6 nanoERG — exactly `DEFAULT_FEE`. A
  peer refill spends 100 % of its value on the transaction fee.
* `DEFAULT_INITIAL_GAS_AMOUNT` (1e9) buys 1157 days at the maximum rate, and about
  15 years on an idle node. It is not a budget, it is infinity.

Three deeper problems sit under the numbers:

1. **Gas has no unit.** `gas_amount_per_call` and `node_advertised_rates` are wire
   contracts: a peer reads `maintenance_max_per_second: 10` from another node and can
   do nothing with it, because nobody defines what one gas is. Advertising rates in an
   undefined unit is noise.
2. **Cost does not scale with what you consume.** `__get_available_supply` collapses
   CPU, RAM and disk into one scalar in [0,1] and multiplies it by `EXECUTION_COST`.
   An 8 GiB instance and a 128 MiB instance on the same node pay almost the same.
3. **Resources cannot be priced independently.** A node with scarce RAM and abundant
   disk has no way to say so.

## Three units, kept apart

The model turns on not conflating three different things.

**MU (monetary unit) is what the node counts in.** Prices, balances and charges are
integer MU, everywhere, so no amount goes through a float. MU has no intrinsic value —
it is the node's own accounting unit, like a ledger's internal cents.

**What an MU is worth belongs to the payment contract, not to MU.** A contract declares
how many MU one of its units buys; that is exactly what `ContractRate.mu_per_unit`
carries on the wire, so a peer reading a price can convert it into money it understands.
It also belongs there *in the code*: the rate and its conversions live in
`src/payment_system/contracts/ergo/rate.py`, not in the accounting core, which names no
ledger. Ergo is the default and currently the only implemented payment system, not a
peg — a peer may accept it, not accept it, or accept others alongside it (see
[`CONCEPTS.md`](CONCEPTS.md), "Where ERG fits"):

```yaml
ledgers:
  ergo:
    payments:
      MU_PER_NANOERG: 1     # one MU is one nanoERG
```

`1` is the sensible default — the simplest mapping for the only ledger there is — but it
is a setting, not a definition, because the next ledger will need its own. ERG↔nanoERG
(1e9) is **not** configurable and lives in the code: it is fixed by the Ergo protocol,
and making it a setting would only allow defining a wrong Ergo.

**What the operator reads and types is a third thing again.**

```yaml
ui:
  DISPLAY_UNIT: erg       # "erg" (default) or "mu"
```

Purely presentational: changing it never changes what anybody is charged. `erg` derives
its rate from the ledger, so it cannot go stale when `MU_PER_NANOERG` changes. Any other
name is declared explicitly, which is the hook for showing a fiat figure later:

```yaml
ui:
  DISPLAY_UNIT: usd
  UNITS:
    usd:
      MU_PER_UNIT: 500000000
      SYMBOL: "USD"
      DECIMALS: 2
```

A rate that moves in the real world is a static number there and **will** go stale —
nothing in the node refreshes it. It only ever affects what is printed.

### Why this is not gas again

Gas was a unit nothing declared a rate for, anywhere. A peer reading `gas_amount_per_call`
had no way to price it, and the shipped constants put charges (1e2) and payments (1e58)
56 orders of magnitude apart, so no charge could ever be settled.

Here the rate is explicit, it travels on the wire with every advertisement, and — since
prices and the rate are now configured separately — the node **checks at startup** that a
reference charge still converts to a non-zero amount on-chain, and warns if it does not.
That specific failure cannot recur silently.

The check lives in one place, `config_validation._warn_if_charges_cannot_settle`, called
from `load_config`. It reads the parsed YAML rather than going through `ConfigManager.get`,
because it runs while that singleton is still being built.

### What the user sees

Never MU, unless they ask for it. Every CLI and log boundary renders the display unit
through `format_mu` (`src/utils/monetary.py`), and every operator-supplied amount is
parsed from it by `parse_to_mu`, which refuses anything that would not land on a whole MU
rather than rounding it.

The exception is `nodo pay <peer> <amount>`, whose amount stays in **ERG** whatever the
display unit says: what it moves is an on-chain ERG transfer, denominated by the ledger
rather than by a presentation preference.

## The price vector

There is no single price, and no conversion between resources. Each dimension carries
its own price, in whole MU:

```yaml
pricing:
  # Recurring, charged every manager iteration.
  RAM_MU_PER_GIB_HOUR:  1000000
  CPU_MU_PER_VCPU_HOUR: 4000000
  DISK_MU_PER_GIB_HOUR:  100000
  NET_MU_PER_GIB:       2000000   # tunnelled traffic, both directions
  # One-off operations.
  BUILD_MU:            10000000
  TUNNEL_OPEN_MU:         10000
  MODIFY_RESOURCES_MU:    10000
  # Scarcity: per resource, never global.
  SCARCITY_MAX_MULTIPLIER: 10        # ceiling of the surcharge when a resource runs out
  SCARCITY_CURVE: 1.0                # 1.0 = linear; >1 stays flat until real scarcity

free_tier:
  CREDIT_MU_PER_NEW_CLIENT: 0        # 0 = no gift; >0 = starting credit
  FREE_WHILE_SCARCITY_BELOW: 0.0     # charge nothing while every resource is under this load
```

Prices are whole MU because MU is the unit of account — there is nothing smaller to
express, and a fractional price is a configuration mistake rather than a rounding
problem, so it is refused.

A price of `0` makes that resource free. `FREE_WHILE_SCARCITY_BELOW` makes the node free
while it is idle and priced once it is not. Together they cover the whole range an
operator may want: expensive, cheap, free, or free up to a point.

### Charge formula

```
charge_MU(Δt) = Σ_over_resources  price_MU_per_unit_second[r]
                                × amount[r]
                                × Δt
                                × scarcity_multiplier[r]
```

`scarcity_multiplier[r]` is computed **per resource** from that resource's own
availability, in `[1, SCARCITY_MAX_MULTIPLIER]`. This is the substantive change from
`maintain_execution_cost`, where one scalar covered all resources and capped the total
at `EXECUTION_COST`; here a node short on RAM raises only its RAM multiplier, and the
charge grows with the instance's actual size.

One-off operations (`BUILD_MU`, `TUNNEL_OPEN_MU`, `MODIFY_RESOURCES_MU`) are flat
and not scarcity-scaled: they price work done once, not occupancy.

`SCARCITY_CURVE` is the exponent applied to the missing supply, so raising it holds the
surcharge back: at `2.0`, a tenth of a resource gone costs 1.09x rather than the 1.90x
linear would charge. Raising `lack` to the *reciprocal* of the curve does the opposite,
and is the shape this shipped with — the two agree only at `1.0`, which was the one
value under test.

### Starting an instance

```
start_MU = BUILD_MU (if it is not built already)
         + the balance the instance starts with
```

The balance is what buys runtime, and it is derived from the requested resources for
`deposits.INITIAL_RUNTIME_HOURS` (`manager.default_initial_balance`) — so a 128 MiB
instance and an 8 GiB one no longer start with the same funding. The maintenance ticks
then spend that balance down.

Occupancy is deliberately **not** charged again as part of the start. Doing both billed
the same window twice: the client paid for the hour once as a start charge that bought
nothing and once as the balance that actually funded it. Worse, the start charge's
occupancy carried the live scarcity surcharge, so the quoted price to start swung with
whatever else the machine happened to be doing that second.

### A quote prices what the instance will hold

`GetServiceEstimatedCost` and the balance above are the only prices computed *before* the
instance exists. Both run the manifest through
`virtualizers.interface.resolve_billable_resources`, which applies the floors the
virtualizer applies when it creates the guest, so a quote and the ticks that follow it
read the same figures:

| Manifest says | Guest gets | Where the floor lives |
| --- | --- | --- |
| no `cpu_period` / `cpu_quota` | 1 vCPU (`100000/100000`) | `limits.resolve_initial_resources` |
| `mem_limit` under `MIN_MEM_MIB` | `MIN_MEM_MIB` (a boot requirement, not a policy) | same |
| `disk_space` under `MIN_ROOTFS_BYTES` | `MIN_ROOTFS_BYTES` | `limits.initial_rootfs_size_bytes` |
| anything, once built | the rootfs image it is handed | the built bundle |

An instance is therefore funded for `deposits.INITIAL_RUNTIME_HOURS` at the prices the
ticks will charge it, and the window it pays for is the window it gets.

Disk is exact for a service already built here — `build.built_rootfs_size_bytes` reads
the size of the image its instances receive — and a **lower bound** otherwise: the
populated-tree floor and the `mkfs.ext4` growth retries are knowable only once the image
exists. A service not yet built here can cost more than its quote, never less.

The floors live in one module, `virtualizers/ch/limits.py`, imported both by the code
that creates a guest (`execute`, `build`) and by the code that prices one. A floor
defined anywhere else would be a price the node charges without quoting.

### Resizing an instance

`ModifyServiceSystemResources` charges the flat `MODIFY_RESOURCES_MU` and asks the
virtualizer to change what the guest holds. What reaches `local_instances` — and therefore
what the next tick charges — is the subset the virtualizer confirms it applied, field by
field (`hotplug._applied_sysreq`, driven by the same per-field report the caller reads
back). A field it declines keeps the value the row already holds, so a resize the guest
never received cannot move the instance's price.

Cloud Hypervisor applies `mem_limit` and the CFS pair, through the instance's cgroup. It
declines `disk_space`: a live rootfs image cannot be resized, so the disk an instance
holds is fixed when it is created.

It also declines the two unbounded forms, `mem_limit=0` and `cpu_quota=0`, which ask for
`memory.max=max` and `cpu.max="max <period>"`. The row stores an amount, and `0` reads as
*no memory* and *no CPU* rather than "unlimited", so a node that granted them would hand
out an unbounded cgroup and charge nothing for it: a resource this model cannot price is
one it cannot promise. `hotplug` fails the call, and the gateway refunds
`MODIFY_RESOURCES_MU` — as it does for any resize that fails.

A shrink is applied as asked, with no floor of the node's own. Booting needs
`MIN_MEM_MIB`, but a running service asking for less memory than that is making a
decision about its own guest, and the row records what it holds either way.

### Worked example

At the defaults above, an instance with 256 MiB RAM, 1 vCPU and 10 GiB disk, on an
idle node (all multipliers 1):

| Window | MU | ERG |
| --- | --- | --- |
| one 10 s manager tick | 14 582 | 0.000014582 |
| one hour | 5 250 000 | 0.00525 |
| one month | 3.78e9 | 3.78 |

Every figure is an integer that fits in int64 with room to spare, and reads as money
without a calculator.

### Rounding

A tick charge is an integer number of MU. At the prices above a tick is in the
thousands of MU, so truncation is irrelevant; only an operator pricing far below these
levels could round a tick to zero, and for them free is the intent. Carrying the
fractional remainder per instance is the exact fix and is **deferred** — it costs a DB
column and is not needed until someone prices that low.

## The on-chain floor

Ergo will not accept an output below its minimum box value, and every transaction pays
a fee:

```
minimum settleable payment = SAFE_MIN_BOX_VALUE (0.001 ERG) + DEFAULT_FEE (0.001 ERG)
```

So ~0.002 ERG is the smallest top-up that can exist, and at 0.002 ERG the fee is 50 %
of what the client gets. Deposits must be sized against this, not against a hand-picked
constant:

| Top-up | Fee overhead | Runtime bought (example instance above) |
| --- | --- | --- |
| 0.002 ERG | 50 % | 23 min |
| 0.05 ERG | 2 % | 9.5 h |
| 0.5 ERG | 0.2 % | 4 days |

`MIN_DEPOSIT_PEER` and `TOTAL_REFILLED_DEPOSIT` are replaced by values **derived** from
this floor and from a target fee overhead, not written by hand. Recommended default
peer deposit: **0.05 ERG**.

That floor governs the **automatic** refill only — `maintain.peer_deposits`, where nobody
named a figure and a transfer worth less than its own fee is simply waste. A command that
was given an amount sends that amount: `nodo pay <peer> 0.001` moves 0.001 ERG. Raising it
silently to 0.05 would contradict `parse_to_mu`, which refuses to round an operator's
figure two calls earlier; and an amount genuinely too small to settle is refused
downstream by `process_payment`, against Ergo's real minimum box value, with a message
saying so.

## Constant migration

There is no balance migration: no node runs in production, so balances reset.

| Removed | Replacement |
| --- | --- |
| `ledgers.ergo.GAS_PER_ERG` | `ledgers.ergo.payments.MU_PER_NANOERG` (1 by default) |
| `EXECUTION_COST`, `EXECUTION_BENEFIT` | `pricing.*_MU_*` price vector |
| `BUILD_COST` | `pricing.BUILD_MU` |
| `MODIFY_RESOURCES_COST` | `pricing.MODIFY_RESOURCES_MU` |
| `TUNNEL_OPEN_COST`, `TUNNEL_COST_PER_KB` | `pricing.TUNNEL_OPEN_MU`, `pricing.NET_MU_PER_GIB` |
| `FREE_GAS_THRESHOLD` | `free_tier.FREE_WHILE_SCARCITY_BELOW` |
| `FREE_TRIAL_GAS_AMOUNT` | `free_tier.CREDIT_MU_PER_NEW_CLIENT` |
| `DEFAULT_INITIAL_GAS_AMOUNT` (+ `_FACTOR`, `USE_`) | derived: price of the requested resources × a configured initial runtime window |
| `MIN_DEPOSIT_PEER`, `TOTAL_REFILLED_DEPOSIT` | derived from the on-chain floor and a target fee overhead |
| `DEV_CLIENT_GAS_AMOUNT` (1e256) | an `unmetered` flag on dev clients — that is what the number was trying to say |
| `INIT_COST_CONFIGURATION_FACTOR`, `MAINTENANCE_COST_CONFIGURATION_FACTOR` | gone; they scaled a unitless number |
| `EXPONENTIAL_COST_FACTOR` (module constant) | `pricing.SCARCITY_CURVE`, applied per resource |

`COST_AVERAGE_VARIATION` and `SOCIALIZATION_FACTOR` are untouched: they belong to peer
selection, not to pricing.

## Files changed

* `src/utils/cost_functions/execution_cost.py` — per-resource scarcity replaces the
  single supply scalar; `maintain_execution_cost` becomes the charge formula.
* `src/utils/cost_functions/general_cost_functions.py` — `node_advertised_rates`
  publishes the price vector in MU per resource-second, which a peer can finally act on.
* `src/manager/maintain.py` — the tick charge; peer deposits derived from the floor.
* `src/payment_system/contracts/ergo/rate.py` — this ledger's rate and its MU↔nanoERG
  conversions, replacing `GAS_PER_ERG`. Deliberately light and separate from
  `interface.py`, because `format_mu` resolves the display unit through it on log lines.
* `src/payment_system/contracts/envs.py` — `display_units()` and `settlement_floors()`
  join the existing per-contract dispatch, so nothing generic names a ledger: `monetary`
  gets its display units from here and `deposits.py` its floors.
* `src/payment_system/ledgers.py` — publishes the rate as `ContractRate.mu_per_unit`.
* `src/balancers/estimated_cost_sorter/estimated_cost_sorter.py` — simplifies: with all
  nodes quoting MU, `erg_per_gas_unit = 1 / (peer_gas_per_erg / local_gas_per_erg)`
  disappears.
* `src/database/sql_connection.py`, `src/database/migrate.py` — the `gas` TEXT columns
  hold MU; rename to `balance_mu`.
* CLI: `nodo increase_gas` / `decrease_gas` / `increase_peer_deposit` become
  `increase_deposit` / `decrease_deposit` and take the display unit; `peers`, `clients`,
  `instances`, `estimate`, `observe`, `pay` render it too.
* `docs/CONCEPTS.md` "Gas" section, `docs/CONFIG.md` cost table, `docs/TUNNELING.md`
  metering paragraph.

## Decided

1. **The proto is renamed off "gas".** Field *numbers* carry the encoding, so renaming
   messages and fields is wire-compatible — this is a source and spec change, not a
   breaking one.

   | Now | Becomes |
   | --- | --- |
   | `GasAmount` | `Amount` |
   | `GasPrice` | `ContractRate` (MU per unit of that contract) |
   | `GasPrice.gas_amount` | `ContractRate.mu_per_unit` |
   | `Service.Api.Slot.gas_amount_per_call` | `mu_per_call` |
   | `Configuration.initial_gas_amount` | `initial_mu` |
   | `EstimatedCost.cost / init_maintenance_cost / max_maintenance_cost` | unchanged names, now `Amount` |
   | `Metrics.gas_amount` | `Metrics.balance` |
   | `ModifyServiceSystemResourcesOutput.gas` | `.balance` |
   | `ModifyGasDepositInput/Output`, `gas_difference` | `ModifyDepositInput/Output`, `difference` |
   | `ObserveEvent.Session.gas`, `.Metrics.gas` | `.balance` |
   | `Gateway.ModifyGasDeposit` RPC | `Gateway.ModifyDeposit` |

2. **The proto copies are unified.** Done ahead of the rename, since a rename applied
   to two drifted copies would have entrenched the split: `src/commands/tui/protos/`
   is deleted and `src/commands/tui/build.rs` compiles `protos/` directly. There is now
   one schema and no way for a second one to drift.

3. **CLI: the commands act on the balance, in the operator's display unit.**
   `increase_gas` / `decrease_gas` become `increase_deposit` / `decrease_deposit` and
   take an amount in `ui.DISPLAY_UNIT` (ERG by default). `nodo pay` is the exception and
   stays in ERG: it moves an on-chain ERG transfer.

## Found while implementing

* **CPU and disk were never billed.** `execution_cost` read `cpu_limit` and
  `disk_limit` off `Sysresources` through `getattr(..., None)`. Neither field exists
  on that message — it carries `cpu_period` / `cpu_quota` (CFS) and `disk_space` — so
  both always read as absent and only memory ever moved a price.

  Fixing the cost function was not enough: the recurring charge prices an instance from
  its `local_instances` row, and that row had no CPU columns at all, while `mem_limit`
  was inserted as a literal `0` and only ever corrected by a later `modify_resources`.
  So the tick billed disk and nothing else, however the prices were set — the per-resource
  model was real in the unit tests and absent in production. `local_instances` now carries
  all four (`mem_limit`, `disk_space`, `cpu_period`, `cpu_quota`), `add_local_instance`
  writes what the instance actually holds, and the tick passes all four through.

  "What it holds" includes disk: the row records the size of the rootfs image the
  instance was actually handed, not the `disk_space` its manifest declared. The build
  floors the image at `MIN_ROOTFS_BYTES`, at the populated tree plus overhead, and grows
  it again on every `mkfs.ext4` out-of-space retry, so the image is routinely larger
  than the declared figure and the declared figure would under-bill it. The manifest is
  still required (a service that names no disk is rejected) and remains the fallback for
  a virtualizer that does not report disk back.
* **`service.json` renames `gas_amount_per_call` to `mu_per_call`.** The packer
  rejects the old key rather than ignoring it: a service that kept it would otherwise
  pack with no per-call price at all, i.e. silently free.
* **`StopService` was parsed as the wrong message.** `manager.stop_instance` read the
  refund with `indices_parser=ModifyGasDepositOutput` and then took `.amount`, a field
  that message does not have; the reply is a `Refund`. Pre-existing, surfaced by the
  rename.

## Still open

* **Fractional remainder carry** — deferred, see Rounding.
