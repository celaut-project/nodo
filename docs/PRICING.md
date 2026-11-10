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
how many MU one *base* unit of its asset buys (a nanoERG, a satoshi, a token's smallest
unit); that is exactly what `ContractRate.mu_per_unit` carries on the wire, so a peer
reading a price can convert it into money it understands. Whole ERG or BTC are only how
an amount is shown to a person. Because the rate is an integer on the wire,
`MU_PER_NANOERG`, `MU_PER_SATOSHI` and an asset's `MU_PER_UNIT` must be whole numbers.
It also belongs there *in the code*: each rate and its conversions live in that
contract's own `rate.py` — `contracts/ergo/rate.py`, `contracts/bitcoin/rate.py` — not
in the accounting core, which names no ledger. Ergo is the default; **Bitcoin is
implemented alongside it** ([`BITCOIN.md`](BITCOIN.md)), and a node may offer either,
both, or neither. Neither is a peg — a peer may accept one, the other, or others
alongside them (see [`CONCEPTS.md`](CONCEPTS.md), "Payment systems").

Two consequences worth knowing before configuring a second one. **Each system's rate is
its own, and they must agree about what an MU is worth**: a satoshi is worth about a
million nanoERG, so `MU_PER_SATOSHI` cannot borrow `MU_PER_NANOERG`'s value, and the
node warns at startup if it does. And **each system's floors are its own** — Bitcoin's
fee and dust limit are orders of magnitude above Ergo's, so a deposit is sized for the
system that will settle it rather than for the most expensive one the node supports:

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

### A rate per asset, not per ledger

A ledger is not a currency. An Ergo P2PK address holds ERG **and** every EIP-4 token sent
to it — one script, one address, one `contract_hash` — so what carries a rate is a
**payment method**: `(ledger, contract, asset)`. Each configured asset declares its own
`MU_PER_UNIT`, in MU per **base** unit, exactly as `MU_PER_NANOERG` is MU per base unit
of ERG:

```yaml
ledgers:
  ergo:
    payments:
      MU_PER_NANOERG: 1
      ASSETS:
        - TOKEN_ID: "003bd19d…"   # 64 hex characters; never a name
          SYMBOL: "SigUSD"
          UNIT_NAME: "sigusd"     # unique across assets and ui.UNITS
          DECIMALS: 2             # stated, not read from the minter's registers
          MU_PER_UNIT: 20000000   # MU per cent
```

Three things follow, and they are the reason this is config and not a lookup:

**`MU_PER_UNIT` is static.** For a stablecoin that is the point. For a volatile token it
means the operator re-sets it by hand, and a stale figure misprices every quote until
they do. Reading it from an on-chain oracle is the obvious follow-up and a separate trust
decision.

**The two rates have to be coherent.** Their ratio *is* this node's opinion about what
the token is worth in ERG, so no price feed is needed to notice that one of them is out
by a power of ten: the node warns when the pair implies a whole token worth less than a
nanoERG (nothing priced in it could settle) or more than a million ERG (`MU_PER_UNIT`
read as "per whole unit"). This matters even for an operator who prices nothing in ERG,
because a token method's fee floor is denominated in ERG while its minimum output is one
base unit of the token — deposit sizing is wrong by exactly the ratio between them.

**`UNIT_NAME` contributes a display unit.** One per asset, derived from the ledger like
`erg` is, so `ui.DISPLAY_UNIT: sigusd` shows every figure in SigUSD. Two assets claiming
one name, or an asset claiming a name already under `ui.UNITS`, is a **startup error**:
the merge is a `dict.update`, so the second would silently win and every amount an
operator reads would be off by the ratio between the two.

**What the operator reads and types is a third thing again.**

```yaml
ui:
  DISPLAY_UNIT: mu        # "mu" (default) or "erg"
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

MU by default, so the figures are not mistaken for ERG actually held; a ledger's unit (`erg`, `btc`) is opt-in. Every CLI and log boundary renders the display unit
through `format_mu` (`src/utils/monetary.py`), and every operator-supplied amount is
parsed from it by `parse_to_mu`, which refuses anything that would not land on a whole MU
rather than rounding it.

The exception is `nodo pay <peer> <amount>`, whose amount stays in the unit of the
**asset being paid** whatever the display unit says — ERG, BTC, whole SigUSD — because
what it moves is an on-chain transfer, denominated by the asset rather than by a
presentation preference. Which asset is named with `--payment-method <ledger>:<asset>`
(or `--ledger` and `--asset`), and it is needed exactly when this node offers more than
one method: the ledger alone stopped being an answer once one Ergo contract could be
paid in ERG and in a token, at different rates. The named method is what the amount is
read in *and* what the payment settles through; without a name, funding is the selection
and the payer settles through the first method it can fund.

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
  STATEMENT_QUERY_MU:       100     # a balance statement, plus...
  STATEMENT_ROW_MU:           1     # ...this for each movement that it returns
  # Scarcity: per resource, never global.
  SCARCITY_MAX_MULTIPLIER: 10        # ceiling of the surcharge when a resource runs out
  SCARCITY_CURVE: 1.0                # 1.0 = linear; >1 stays flat until real scarcity
  PRICE_BOUNDED: true                # quotes promise a price bound (price_bounded)
  PRICE_VOLATILITY: 0.10             # v: at most +10 % per period; omit to compute it
  PRICE_VOLATILITY_PERIOD_HOURS: 24  # T; 0 = one period for the whole life
  PRICE_VOLATILITY_WINDOW_HOURS: 168 # window of the computed v (default 7 periods)
  PRICE_VOLATILITY_MIN: 0.05         # floor of the computed v

free_tier:
  CREDIT_MU_PER_NEW_CLIENT: 4950000  # one hour of 0.5 GiB + 1 vCPU at the prices above, at 1.10x
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

A balance statement (`GetBalanceStatement`, #570) costs `STATEMENT_QUERY_MU` plus
`STATEMENT_ROW_MU` for each movement that it returns, charged to the account that asks
and written as its `statement_query` movement before the node answers. The fixed part
pays for the call, the authentication and the signature. The rest follows the rows: the
key on `(account_id, seq)` finds where a statement starts at the same cost whatever the
size of the account, and what grows is reading, signing and sending each row. Reading only
the head of an account asks for no row and pays the fixed part. A node that delegates
reads its own account on each peer, and the statement of each child, every
`ledger.STATEMENT_READ_EVERY_TICKS` ticks, so these prices are the cost to a peer of
checking this node. A read asks for no more rows than the balance of the reader pays for,
and the peer may charge the account only for the statements that it asked for.

The balance of a client alone is not a statement. `GetClientMetrics` (#597) answers it, takes a `Client`, and is free: a node reads its balance on a peer on each tick, and a node whose balance is zero must still be able to read that it is zero.

`SCARCITY_CURVE` is the exponent applied to the missing supply, so raising it holds the
surcharge back: at `2.0`, a tenth of a resource gone costs 1.09x rather than the 1.90x
linear would charge. Raising `lack` to the *reciprocal* of the curve does the opposite,
and is the shape this shipped with — the two agree only at `1.0`, which was the one
value under test.

### Bounding the price of a running instance

Scarcity is read from the load of the moment and applies to every running instance,
whenever it was launched. A resource that is scarce now is scarce for everybody who holds
it. Without a bound, an instance that was funded at 1x can be charged up to
`SCARCITY_MAX_MULTIPLIER` when the node fills up. With the shipped values it then runs out
in about six minutes instead of one hour. It is stopped. The
service takes no reputation penalty for this death.

The price bound of a launch bounds that (#636, which replaces the `MAX_PRICE_VOLATILITY`
of #335). A quote with `price_bounded` true promises that the price of the instance
rises by at most a factor `(1 + v)` per period `T`, counted from its launch. At launch,
the node records in `local_instances` the scarcity multiplier of each resource
(`launch_cpu_bp`, `launch_mem_bp`, `launch_disk_bp`, in basis points) and the bound that
it read (`launch_volatility` = v, `launch_period_seconds` = T). Each tick then charges,
per resource:

```
k             = floor((t - launched_at) / T) + 1      (k = 1 when T = 0)
multiplier[r] = min(current_multiplier[r],
                    launch_multiplier[r] * (1 + v)^k,
                    SCARCITY_MAX_MULTIPLIER)
```

`t` is the end of the interval that the tick charges, so an interval that spans a change
of period has the higher `k`. The tick uses the values that the row stored, never the
config or the price history of a later moment, so the ceiling of an instance does not
move during its life.

- `pricing.PRICE_BOUNDED: false` promises nothing: the row stores NULL, and the price of a
  running instance follows the load up to `SCARCITY_MAX_MULTIPLIER`. Peers rank such a
  node after the bounded ones.
- `PRICE_VOLATILITY: 0` fixes the multiplier for the life of the instance.
- `PRICE_VOLATILITY_PERIOD_HOURS: 0` is one period for the whole life. With
  `PRICE_VOLATILITY: 1.0` it is the old `MAX_PRICE_VOLATILITY: 2.0`.
- Invalid values (NaN, infinite, negative; a window of 0) refuse to load, and so does
  the removed `MAX_PRICE_VOLATILITY`.

**The computed volatility.** Without `PRICE_VOLATILITY`, the node computes `v` from its own
history (`price_history`, one sample a minute of the multiplier that a launch would record,
1x while free). For every stretch of length `T` in `PRICE_VOLATILITY_WINDOW_HOURS`, the rise
is the highest multiplier in the stretch over the one at its start, less 1, for the
resource that rose the most; a fall is 0. `v` is the p95 of those rises, and never less
than `PRICE_VOLATILITY_MIN`. There is no cap. With no history, a history shorter than `T`,
or `T = 0`, `v` is the floor. A quote and a launch read it once each, and the launch stores
what it read.

**The bound is a ceiling only.** It is not symmetric, for this reason: the risk that it
covers is a rise that the funding of the instance did not cover. A fall of the load
reaches a running instance in full, so a client never pays more than the current load
asks. A symmetric bound would also keep the price up while the node empties out. The
cost is a one-way bet in favour of the client, which the node accepts, because the
instance is funded at the ceiling.

Three more facts:

- **A launch while the node is free is anchored at 1x.** An instance launched while
  `free_tier.FREE_WHILE_SCARCITY_BELOW` made the node free records 1x (the base price)
  for each resource. It pays nothing while the node stays free; once the node crosses
  the threshold its multiplier may rise to the ceiling over 1x, and no further. Its
  balance is funded for that ceiling (see "Free node" below).
- **Only the multiplier is bounded.** A change of the base prices (`RAM_MU_PER_GIB_HOUR`
  and the others) reaches a running instance in full.
- **A row with no recorded launch price or bound is not bounded** (a dev instance). It is
  charged at the current load. An instance that a peer runs for you (delegated) is charged
  by this node at the multipliers of its launch for its whole life (#577): a fixed price,
  which the quote of this node states as bounded with `v = 0`.

The clamp is in `execution_cost.maintenance_charge_mu`, not in the maintenance loop. The
tick takes `k` from the time since the launch at the end of the interval that it charges
(`PriceBound.period_index`), and the funding sum takes `k(i)` from the same function
(`price_bound.tick_period_index`): the tick `i` and the period `i` of the sum are in the
same volatility period, so a balance funded for `N` periods covers the ceilings of the first
`N` ticks (#636, 6).

**What the quote shows.** `GetServiceEstimatedCost` has these figures:

| Field | Meaning |
|---|---|
| `init_maintenance_cost` | One tick for the resources at the start, at the launch price of now (`P`). |
| `max_maintenance_cost` | One tick for the maximum resources, at the same price. Neither figure is a limit by itself. |
| `price_bounded` | `pricing.PRICE_BOUNDED`. True: a tick never charges more than the figure of the resources held times `(1 + price_volatility)^k`. |
| `price_volatility`, `volatility_period_seconds` | `v` and `T` of the bound. Meaningless when `price_bounded` is false. |
| `closes_at` | Unix time of the next scheduled close of the activity window (`activity_window.next_close`), 0 if none is scheduled. An absolute time. It comes from the configuration of the node and never from its load. **It is not a promise that the instance lives until this time** (the node can stop for a cause that is not in its schedule), and **a value of 0 is not a promise of a long life**. |
| `on_close` | What the node does with a running instance when the window closes: `ON_CLOSE_REFUSE` (nothing; the default, so an older node reads as this), `ON_CLOSE_STOP` or `ON_CLOSE_HIBERNATE`. A node with no window says `ON_CLOSE_REFUSE` whatever `ON_CLOSE` holds. The host keeps both values in the row of the instance (`launch_on_close`, `launch_closes_at`), and a delegating node keeps those of the peer (`quote_on_close`, `quote_closes_at`). |
| `cost` | What the start spends: the build, plus the builds of the initial children. No balance (#636, 9). The client adds the balance for the periods that it chooses (the funding sum below). |

A node that delegates to this one checks the charges of the child against the bound
(`MAINTENANCE_OVERCHARGED`, docs/REPUTATION.md). A quote with `price_bounded` false is
never checked.

**Free node (#636, H and 2).** While the node is free the maintenance figures of the quote
are at the anchor (1x), not 0. The builds are free, so `cost` is 0, and the funding sum asks
for the balance as for any start: a start with no `initial_mu` gets the sum for
`deposits.DEFAULT_INITIAL_PERIODS` periods over the anchor, and a start must hold at least
one period. There is no special case for a free node in `compute_start_service_cost`. The
ticks charge nothing while the node stays free, and the unspent balance goes back to the
father at the stop. Before, a free start got a balance of 0, and with `costs.ALLOW_DEBT`
off the instance was stopped at its first tick after the node left the free state (#323).

The quote and the funding are exact only when the load does not move between the quote and
the launch. A launch at a higher load than its quote records a higher launch price and
is funded for it.

### Starting an instance

The caller pays `initial_mu` and nothing else (#563). Every node takes the cost of the
build from the balance of the instance that it starts. No node takes it from the account
of the caller.

```
balance of the new instance = initial_mu - BUILD_MU (if it is not built already)
```

The most that a start can cost the caller is therefore `initial_mu`, less what the
instance does not spend and the refund when it stops. The caller does not add the build
to its own estimate.

Two rules apply, and the node rejects the start and charges nothing if one fails:

- **The build must fit.** `initial_mu` is at least the build.
- **Minimum runtime.** The balance after the build is at least one maintenance period
  (`MANAGER_ITERATION_TIME`) with the resources at the start: the funding sum for one
  period, `init_maintenance_cost × (1 + v)^k(1)` when the quotes are bounded, and the
  launch price when they are not (`start_funding.minimum_runtime_mu`). So `cost` plus the
  sum of one period passes. Without this rule, a caller could pay the build and get an
  instance that dies at the first tick.

`EstimatedCost.cost` is what the start spends: the builds, with no balance (#636, 9). A
caller funds the start with `cost` plus the funding sum for the `N` periods that it chooses
(below). If the request has no `initial_mu`, the node does the same for its own `N`:

```
initial_mu (default) = cost
                     + Σ_{i=1..DEFAULT_INITIAL_PERIODS} init_maintenance_cost × (1 + v)^k(i)
```

`deposits.DEFAULT_INITIAL_PERIODS` is 360 in `config.example.yaml`: one hour at a period of
10 s (`start_funding.default_balance_mu`). The balance is derived from the requested
resources, so a 128 MiB instance and an 8 GiB one do not start with the same funding. The
maintenance ticks then spend that balance down. Until #636, `cost` already held a balance
for `deposits.INITIAL_RUNTIME_HOURS`, and the funding sum added periods on top of it: a
client that followed the formula paid those hours two times. `INITIAL_RUNTIME_HOURS` and
`manager.default_initial_balance` are removed, and a config that still sets the key is
refused at load.

**The initial children of the service (#538).** A service pays for the instances that it
starts out of its own balance: each start takes its `initial_mu` from the father (a
`child_start` movement). The maintenance of a child is not paid by the parent: the child has
its own account, and its `child_start` pays its first balance. So `EstimatedCost.cost`
includes the builds of the children of its `initial_environment_workload` (see
`PACKING.md`). There is no separate field: a client has nothing to do with the split.

```
cost = BUILD_MU (if it is not built already)
     + initial children
```

The initial children are priced so:

```
one workload    = count × BUILD_MU of the child (if it is not built already)
one group       = the sum of its workloads
one clause      = the dearest of its groups      (the service chooses the group at run time)
initial children = the sum of the clauses
```

No balance of a child is in `cost` (#636, 9). Each child gets its balance in its own
`StartService`: the `initial_mu` that the parent sends, or with none the default of the node
that runs it (`cost` of the child plus the funding sum for `DEFAULT_INITIAL_PERIODS`). The
parent pays that `child_start` out of its own balance, so a client that funds the parent for
`N` periods funds the starts of the children from the same balance: a parent whose children
start with the default balance needs more than one period of its own. The build is 0 when the
workload names no service (neither a `hash` nor a complete `service`), when the child is
built on this node, and while the node is free (`start_funding.initial_children_mu`). A child
that declares its own `initial_environment_workload` adds the builds of its own children.

- The quote does not probe peers. It uses all the groups of a clause, not only the ones that
  fit now. This can over-quote. An over-quote is safe: the balance that the instance does not
  spend goes back to the parent when it stops.
- The quote uses the **local price**. A child that runs on a peer has the price of that peer.
  This is out of scope, as for the service itself (see "This node delegates only at no loss").
- `possible_environment_workload` has no cost. It is in the admission only.
- Children that the service starts later, and that are not in `initial_environment_workload`,
  are not in the quote. The client covers them with top-ups (`nodo increase_deposit`).
- `cost` does not include the balance or the maintenance of the children. There is no
  `children_maintenance_cost`.

The funding formula for a client (the comment of `EstimatedCost.cost`), when the quote is
bounded:

```
initial_mu = cost
           + Σ_{i=1..N} init_maintenance_cost × (1 + price_volatility)^k(i)
k(i)       = floor(i × maintenance_seconds_loop / volatility_period_seconds) + 1
             (k(i) = 1 when volatility_period_seconds = 0)
```

The client chooses `N`, at least 1. The sum funds the resources at the start only. A rise
from `at_init` to `at_most` is paid when the resources change
(`ModifyServiceSystemResources`). `nodo estimate` prints `cost` plus this sum for 1 h, for
24 h and for `DEFAULT_INITIAL_PERIODS` (`price_bound.funding_sum_mu`). An unbounded quote
gives no limit, so no sum is sufficient. The parent is free to give a different
`initial_mu` to a child in its own `StartService`.

The ticks round each resource down to a whole MU, and the sum rounds its total up, so the
two agree to within a few MU per period when the price sits at the ceiling. A tick charges
the seconds since the last charge, so a tick that comes late charges a little more than one
period (see below).

**The funding check.** When the request has an `initial_mu`, `launch_service` refuses the
start — after the admission and before any charge — if `initial_mu` less the build of this
node is less than the initial children plus one maintenance period of the parent
(`start_funding.minimum_runtime_mu`). The message gives the least `initial_mu`. Before this,
the parent started, failed to start its children and retried in a loop (#628). The setting
`workload_admission.ON_UNDERFUNDED` is `reject` (default) or `warn`. A service with no initial
children is not checked here; the two rules above apply as before.

The check uses the functions of the quote (`initial_children_mu`, the build and the
period) at the **base load** (`execution_cost.base_load`): no load surcharge on any resource,
and not free. A client quotes first and starts later, and the load can move between the two.
Every price rises with the load, so a quote at any load, while the node charges, is at least
the figure of the check. This is the drift rule:

- A start funded with the `cost` of any earlier quote of this node, plus the funding sum of
  at least one period of the parent, passes the check, at any load of the start.
- At the base load, the check asks for exactly the builds of the children that the quote put
  in `cost`. The balances of the children are not checked: the parent pays them from its
  own periods when it starts them.
- While the node is free at the start, the builds are free and the start is not checked. A
  quote taken while the node was free gives `cost` = 0; if the node charges at the start,
  the check can refuse it for the builds.
- The check does not ask for the load surcharge. The real `child_start` is priced at the load
  of the moment that the parent starts the child, as for any start. If the load is up then,
  the parent pays more than the quote; the client covers this with top-ups.
- **The drift of a computed `v` (#636, 7).** With `PRICE_VOLATILITY` unset, the node
  computes `v` from its price history, and a quote and a start read it at different
  moments. A `v` that rose in between makes the check of one period ask for a little more
  than the quote: `init × (1 + v_start)` against `init × (1 + v_quote)`. A start funded with
  exactly `cost` plus one period of an earlier quote can then be refused. A client that
  funds a few more periods than the least, or quotes again just before the start, is not
  affected. The default balance of a start with no `initial_mu` reads `v` at the start, so
  it is not affected either. A fixed `PRICE_VOLATILITY` has no drift.

Before this rule, the check priced the children at the load of the start, at the worst case
of the volatility bound. With the CPU busy at the start, it asked for up to
`SCARCITY_MAX_MULTIPLIER` times the base price (41.75M MU against quotes of 9.7M–27.2M on a
test node) and refused starts funded exactly as the quote said.

When the request has no `initial_mu`, the father pays the `cost` of the candidate plus the
default balance of the service alone (`launch_service._client_charge_mu`). When the service
runs on a peer, this node keeps only its build (`delegated_price.own_build_of` takes the
builds of the children that its price put in `cost` out again) and sends the rest to the
peer. The peer takes its whole `cost` (its build and the builds of the children,
`delegated_price.peer_build_mu`), and what is left must pay one period as the peer quoted it
(the funding sum of one period at its bound). A local start with no amount at all (a direct
call) is funded with the same figure.

A tick charges a local instance for the seconds since its last charge, not for a fixed
`MANAGER_ITERATION_TIME`: a pass of the manager lasts as long as its work does (a revive
waits for a guest's boot), and the time over the interval used to be unbilled. It charges
at least one interval, which is what the first charge after a launch, a revive or a
restart of the daemon is, and at most six (`maintain.LOCAL_TICK_MAX_INTERVALS`), so that a
host that stalled does not bill a span in which no guest ran. The `maintenance` movement
carries the seconds it priced, which is what the father checks it against (#570).

When the quotes are bounded, the default balance prices each period at the ceiling of the
volatility period that its tick is in (see [above](#bounding-the-price-of-a-running-instance)):
the launch price times `(1 + v)^k(i)`, which is just `1 + v` while
`DEFAULT_INITIAL_PERIODS × MANAGER_ITERATION_TIME` is less than `T` (1.10x at an idle node
with the shipped values: 360 periods of 10 s in a period of 24 h). A rise of the price
inside the bound cannot stop the instance before those periods.
`free_tier.CREDIT_MU_PER_NEW_CLIENT` follows, or a new client is refused at its first
launch (issue #324).

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

An instance is therefore funded for `deposits.DEFAULT_INITIAL_PERIODS` periods at the prices
the ticks will charge it, and the window it pays for is the window it gets.

Disk is exact for a service already built here — `build.built_rootfs_size_bytes` reads
the size of the image its instances receive — and a **lower bound** otherwise: the
populated-tree floor and the `mkfs.ext4` growth retries are knowable only once the image
exists. A service not yet built here can cost more than its quote, never less.

### A read-only service is priced at its image, with no floor

The rootfs floors above exist to leave room for writes. A service that declares
`read_mode: ro` (see docs/PACKING.md) has none to leave room for: its image is squashfs
or erofs, the guest mounts it read-only, and the only writable thing it is given is a
tmpfs on `/tmp` and `/run`. So `MIN_ROOTFS_BYTES`, `OVERHEAD_BYTES` and the
`MKFS_GROWTH_FACTOR` retries are all skipped for it, and it is priced at the size of the
image actually built.

This is where the saving is. `OVERHEAD_BYTES` is not proportional — 64 MiB whether the
tree is 2 GiB or 2 MiB — so it is a rounding error on a large service and the dominant
term on a small one. On the capsules measured in issue #369 it was 44% of a 145 MiB
image; on a 12 MB service the floor arithmetic came to `max(128 MiB, 12 MB + 64 MiB)`,
a 10x multiplier. Compression is on top of that.

`at_init.disk_space` also changes meaning, in the direction the field reads like it
already means. On the writable path it is a **floor**: the image is grown to it, and the
instance is billed for the declaration rather than for its bytes — which is how the pdf
capsule in #369 came to hold 1 GiB of ext4 for a 221 MiB tree. On the read-only path it
is a **ceiling**: nothing is grown to meet it, the instance is billed for the image, and
a build whose populated tree exceeds it is refused outright rather than built oversized.

Both halves need the manifest, not just the hash, so `resolve_billable_resources` takes
an optional `service`. Without it the floors stay — which over-quotes rather than
under-quotes, and only over-quoting is recoverable.

The floors live in one module, `virtualizers/microvm/limits.py`, imported both by the code
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

It also declines the two unbounded forms, `mem_limit=0` and `cpu_quota=0`, which would
write `memory.max=max` and `cpu.max="max <period>"`. The row stores an amount, and `0`
reads as *no memory* and *no CPU* rather than "unlimited", so a node that granted them
would hand out an unbounded cgroup and charge nothing for it. The gateway never passes
them to `hotplug`: `0` is the request to hibernate (next section).

A shrink is applied as asked, with no floor of the node's own. Booting needs
`MIN_MEM_MIB`, but a running service asking for less memory than that is making a
decision about its own guest, and the row records what it holds either way.

### Hibernating an instance

A resize that leaves an instance with no CPU (`cpu_quota=0`) or no RAM (`mem_limit=0`)
hibernates it (#498): the node stops the VM and keeps the disk, the row, the address and
the token. It is the state of a suspended instance (#594) with `suspended_reason`
`hibernated`, and it differs from the others in three ways. Nothing but a resize brings it
back: a top-up does not revive it, and no suspension grace ends it. It is charged for its
disk and nothing else, the same `compute_maintenance_cost` with `0` CPU and RAM. One that
cannot pay it keeps its disk for `instances.SUSPENDED_GRACE_SECONDS`, counted from the tick
that found it unable (`hibernated_out_of_balance`): a top-up that lets it pay makes it
`hibernated` again, and when the grace ends unpaid it is stopped, disk included. With no
grace configured it is stopped at once.

A field the request leaves out keeps the value of the row, and a hibernated row holds `0`
for both, so a request that sets only the CPU or only the RAM of a hibernated instance
leaves it hibernated. A request that leaves both above `0` wakes it: the same admission as
any growth (the service pool and the host limits), then the VM starts again on its own
disk, address and ports. The entrypoint runs again and the process state is lost. A wake
that does not fit leaves it hibernated and refunds the call.

The request may carry `service_token`. The node then changes that instance, whoever sends
it, and the instance pays `MODIFY_RESOURCES_MU`, not the caller. While the father of the
instance is a client, that client must send its `Client` too, as for `StopService`
(#597). Without a token the instance that sends the request, found by its address,
changes itself; a hibernating instance is answered before its VM stops.

The activity window can hibernate an instance too (`activity_window.ON_CLOSE: hibernate`,
#344). Its `suspended_reason` is then `hibernated_by_window`, which is not `hibernated`: the
client did not ask for it. The node saves the CPU and RAM of the instance, and wakes it with
them on the first tick after the window opens, under the same admission as any wake. If there
is no room it stays hibernated and the next tick tries again. A wake by hand while the node
is closed is refused with the reason of the window. An instance that a client hibernated is
never woken by the node. If one that the window hibernated cannot pay its disk, it becomes
`hibernated_out_of_balance` and the node no longer wakes it by itself. Hibernation stops
the VM and does not freeze it: memory and processes are lost, so work that cannot restart
from its disk is lost.

The node refuses to hibernate an instance that exports or mounts a shared filesystem, or
that cannot be started again from its own disk, and refunds the call. Wake on incoming
connection and saving the RAM are not part of it.

### An instance that runs on a peer

A delegated instance is charged to its father what this node advertises (#577), like a
local one: our cost functions, its resources and our rates. Nothing
of the peer reaches the father: the rows of its ledger are those of a local instance, and
a peer that raises its prices does not raise the price of the instance. Seen from the
service, it is one more instance of this node.

The **margin is the difference**: what this node charges the instance, less what the peer
charges this node for it (read from its statement, #570 and #571). It is not stored as a
figure: it is the movements of the child here, against `charged_peer_mu`. A peer that
raises its prices after the delegation is a loss of this node: the instance is charged
our price, and it is not moved or stopped for it. A peer that charges more maintenance
than the quote that it gave for the instance loses reputation (#567): the delegated row
keeps that quote in the MU of the peer, and each statement of the child is checked
against it (see
[`REPUTATION.md`](REPUTATION.md#a-child)).

The father pays `initial_mu`, as for a local instance (#563). This node keeps its own
build (`start_build_mu`), and sends the peer the rest of the amount, in the MU of the
peer: the balance of the child, as a local one has it. The peer takes its own build from
that amount, and the rest is the balance of the child on the peer. That is the whole
deposit of the child, so that the instance does not die on the peer while it has balance
here. What it does not use comes back to this node when the instance stops.

- The amount must fund the build of this node and one maintenance period of ours, and
  what is left must fund the build and one period of the peer, as it quoted them. If it
  does not, nothing is charged to the father. A peer that rejects the start for a small
  amount is not at fault, and this node does not lower its reputation.
- `peer_balance_mu` is the balance of the child in its statement on the peer, right
  after the start (#570). It is not the amount that this node sent, because the peer
  already took its build from it. What the peer took for the build is not judged: the
  child holds what is left, and the chain of its statement is the only thing that is
  checked (see
  [`REPUTATION.md`](REPUTATION.md#what-a-peer-charges-from-its-statements)).
- `balance_mu` of the delegated row is what the father can get back: `initial_mu` less
  the build of this node, in local MU. The row opens with a `start` and a `build`, and
  names nothing of the peer.
- The peer must not take anything from the account of this node for the start. The only
  source is the amount that this node sent.

**This node delegates only at no loss.** The balancer compares what this node charges, by
its advertisement, with what the peer quotes, converted to our MU, and a peer is a
candidate only if the first covers the second: the start (the builds and the initial
children), and the
maintenance per second at the start and at the most that the service may grow to (each
node prices a period of its own `MANAGER_ITERATION_TIME`). `GetServiceEstimatedCost` is always the price of this node: the local price if it
has room, and its own price, which it charges for a service that a covered peer runs,
if it has not. The advertised rates do not yet include an estimate of what the peers
charge: an operator that delegates a lot has to price for it. The same is true of the
initial children in `cost`: it is the price of this node for them. The start compares the
totals (build and children) of both quotes, and the peer gets the children with the
balance, so they are paid once.

There is no version check and no fallback for an old peer. An old peer takes the build
from the account of the caller, and also takes the whole amount. Its statement shows
that in our account on it, which nothing but the start of a child of ours may take MU
from.

Apart from the start, the two accounts have nothing to do with each other. There are
two accounts, in two units, and this node stands between them:

| Account | Unit | Who moves it |
| --- | --- | --- |
| the child's deposit, on `delegated_instances.balance_mu` | our MU | the father pays `initial_mu` at StartService; the row holds what he can get back; the maintenance tick spends it down by our price, as for a local instance; the stop refunds what is left |
| this node's deposit with the peer, against the client row it holds for us there | the peer's MU | the peer takes the amount that this node sent for the start, charges the child's real cost against the balance of the child, and refunds the unspent part when the child stops |

The father's claim is the first row and only the first row. It is opened with what he
can get back: `config.initial_mu`, in our MU, less the build of this node (the one-off
start charge, borne exactly as a local one is). It is spent by
`maintain.maintain_delegated_instances`, at our price: the shape of the instance (kept on
its row, as `local_instances` keeps it) and our rates, for the time since the last charge.
Two things are not as for a local instance, because its load is on the peer and not here:
it is **never free** (`free_tier` gives away the capacity of this node, which it does not
use, while the peer charges this node for it), and it is priced at the **scarcity of its
launch** for its whole life (`execution_cost.charge_at_launch_price`). The time is charged
also when the peer cannot be reached: our price does not need it, and an instance that
cannot pay is stopped all the same. So is the last charge before a stop.

The service changes its resources on the peer: `ModifyServiceSystemResources` goes to the
node that runs it, and this node does not see it. It sees it in the statement of the
child, where each `maintenance` movement says the resources and the seconds that the peer
priced. The floors of the two nodes are not the same (the disk of an image that only one
of them has built, `MIN_MEM_MIB`, the vCPU of a service that declares none), so the
figures of the peer are not compared with ours: only their **changes** are taken. The
first period that a statement shows is the start on the peer (`peer_base_*`), and the
shape of the child is our shape at the start (`base_*`) changed as much as the resources
on the peer changed since then. A change before the first period of the peer is not seen.

So the ticks charge the **last shape that is known**, and each statement that is read
corrects the periods that it covers: our price for the shape of the child, less our
price for the shape that was charged (a `maintenance` row with `adjustment` in its
detail, positive or negative). The last shape goes on the row for the next ticks. Each
`resource_change` of the child that the peer did not give back is charged at our
`MODIFY_RESOURCES_MU`. The shape is held down to the most that the service can hold: the
`at_most` of its manifest, or its start when it declares none. A peer that writes more
cannot raise what this node charges its own client. A manifest that declares no
`at_init` is charged the floors of this node, as a local instance is. When the instance stops, `stop_instance` hands
the remainder back through the same `credit_father` a local stop uses — after the row
is deleted, so a stop that is retried cannot pay twice.

What the peer charges this node is **measured, not predicted**, and it is the cost of the margin, not the price of the instance. The row keeps four totals, in the
peer's MU: what the statements that were checked show the child spent
(`verified_consumption_peer_mu`, every movement but those of its father), the top-ups of
its father less the withdrawals among them (`verified_father_net_peer_mu`), the same for
all the `ModifyDeposit` that this node has sent (`father_net_peer_mu`), and what the peer
is measured to have charged for it (`charged_peer_mu`). A *mark* is the last movement of the child's statement
that was checked, with its hash and the balance after it (`peer_balance_mu`). What the
peer has charged is

```
owed   = verified + (balance at the mark - balance now) + (sent - verified sent)
charge = owed - charged
```

The balance now is read at every tick from the `Metrics` of the peer, which is free: what
it has fallen by since the mark is what the child consumed, once what its father moved is
taken out. Every `ledger.STATEMENT_READ_EVERY_TICKS` ticks the statement is read as well,
and what its movements say replaces the estimate for what they cover. A late statement
corrects an early estimate in either direction, and only a statement that continues the
mark gives MU back: a balance that the peer reports could credit the child with a lie, so
a charge that comes from a balance alone is never negative. What this node has sent is
read before the peer is asked, so a `ModifyDeposit` that lands in between can only make a
charge smaller for a moment, and no instance is stopped for what its father caused.

The `Metrics` also say whether the peer has the child suspended (`suspended`, issue #594):
its deposit there ran out, and the peer keeps its disk but runs nothing. While it says so
the row here is suspended too (`delegated_instances.suspended_at`) and the tick charges the
father nothing for the time; `charged_until` still moves, so that time is never charged
later. A top-up of the father reaches the peer, which starts the child again, and the row
follows at the next reading. A child that is still suspended after
`instances.SUSPENDED_GRACE_SECONDS` here is stopped, with a refund, whatever the peer does.

A quote frozen at delegation would drift three ways that are all invisible from here — the
peer repricing its resources, the two `mu_per_unit` rates moving against each other, and a
`hotplug` making the child bigger than the shape it was quoted at. The sum happens on the
peer's scale before any conversion, so a rate that moved between two readings cannot read
as consumption. A correction only moves the totals: the child was not charged for it.

When the statement does not continue the mark, nothing can be checked up to its head. What
the balance says is taken for what was spent, never below zero, and the mark goes to the
head. A fault of the peer is scored; a gap that its retention explains is not.

The stop reads the child's statement one last time before `StopService`, because its
account on the peer ends there, and closes the charge with it. `StopService` carries this
node's client on the peer, which is the father of the child there (#597).

An instance that consumed more than its deposit holds is stopped, and its row is
emptied first rather than refunded: every MU on it is already owed to the peer for
runtime that happened, so what the node absorbs is only the part the deposit could not
reach. That reading is also the liveness check. A peer that answers but no longer knows the
instance has stopped it on its own, and the row here follows it down; a peer that
cannot be reached loses nothing of what it charged, because the totals only advance
with a reading that succeeded, so the next one measures everything since. The
charge, the totals and the mark are written in one transaction (`settle_delegated_tick`).

What the peer refunds at that moment is the *other* account. It is credited to this
node's deposit there, in the peer's MU, and is logged rather than forwarded: paying a
local father out of it would pay him from an account that is not his, on a scale that
is not his, on top of the deposit he is already owed. The node buys the runtime
wholesale from the peer and sells it at the price it quoted, and the difference
between the two meters is the node's position to hold.

Top-ups move both halves. `ModifyDeposit` charges the father here, converts the figure
to the peer's MU (`mu_conversion.convert_mu`, rounding the direction that cannot pay
the client's costs out of the node's pocket) and forwards it — and only writes the
local rows once the peer has confirmed its side, together with the net that it has sent
(`father_net_peer_mu`), in one transaction. The call to the peer carries this node's
client there, for a top-up and for a withdrawal (#597). The peer writes the change in the statement of
the child, and the tick leaves it out of the consumption.

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
| `DEV_CLIENT_GAS_AMOUNT` (1e256) | an `unlimited` flag on clients: the maintenance thread tops them up to `client.UNLIMITED_TARGET_BALANCE_MU`, a finite figure — that is what the number was trying to say |
| `INIT_COST_CONFIGURATION_FACTOR`, `MAINTENANCE_COST_CONFIGURATION_FACTOR` | gone; they scaled a unitless number |
| `EXPONENTIAL_COST_FACTOR` (module constant) | `pricing.SCARCITY_CURVE`, applied per resource |

`COST_AVERAGE_VARIATION` and `SOCIALIZATION_FACTOR` keep their roles unchanged, and have
moved to the `balancers:` block with the rest of the peer-selection formula — which is
what these two paragraphs were already arguing for. They belong to peer selection, not
to pricing.

Peer selection is now three terms rather than two: price, reputation, and donation
credit, plus an explicit `LOCAL_BIAS` for how much this node prefers running work itself.
Each weight is the maximum equivalent price discount, because price enters the score as a
logarithm. See [`REPUTATION.md`](REPUTATION.md) for the formula worked through at the
shipped defaults, [`CONFIG.md`](CONFIG.md#balancers) for every key, and
[`DONATIONS.md`](DONATIONS.md) for what the donation term is and why it is bounded.

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
   take an amount in `ui.DISPLAY_UNIT` (MU by default). `nodo pay` is the exception and
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
