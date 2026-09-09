# Donations

A donation is how a node operator funds the development of the software they run.
`nodo` donates **2 % of what a node earns** by default, and this document exists because
a non-zero default is only honest if the operator can see exactly what it does and turn
it off in one edit.

Two things make this different from a donate button:

- The money is a share of **incoming payments** — earnings, not savings. It has nothing
  to do with the cold-wallet sweep, and it works on a node with no cold wallet at all.
- What a donor gets back does not come from their own node. It comes from **other
  nodes**, which read donations off the chain and weigh them when they choose whom to
  delegate work to.

## Why the incentive has to come from other nodes

`nodo` is open source. An operator can patch out anything the node does to itself, so
the only donation scheme that survives contact with a fork is one where the reward is
granted by somebody else:

- Anything a node grants *itself* for donating is forkable — delete the check, keep the
  bonus.
- Anything other nodes grant a donor is not, **provided they compute it from the chain
  and never from what the donor claims**. A donation from a node's wallet to a known
  address is public, attributable and unforgeable, and the operator cannot award it to
  themselves.

So a donation is a publicly verifiable signal, and the consumers of capacity — the
nodes that delegate work and pay for it — choose to weigh it. Donating buys a better
position in other nodes' routing decisions, which is real revenue. A node that stops
weighing donations only hurts its own peer selection.

Nothing about donations ever travels over the protocol. A peer telling us how much it
donated would be self-declared, therefore forgeable, and therefore worthless.

## Reputation and donation credit are not the same thing

| | earned by | purchasable | in the balancer |
|---|---|---|---|
| **Reputation** | behaving well — paying, returning instances, staying up | no | bonus *and* penalty |
| **Donation credit** | contributing money, verifiably on-chain | yes, that is the point | bonus only |

They must never be merged. If reputation weighed money, reputation would become
purchasable, and with it access to delegated work: a well-funded actor would become the
most "reliable" peer in the network without having served a single instance.

## The two wallet lists

Both live under their ledger in `config.yaml`, because an address only means anything on
its own chain.

```yaml
ledgers:
  ergo:
    payments:
      DONATION_PERCENTAGE: "0.02"
      DONATION_WALLETS:                     # who we FUND
        - { address: "9gGZ…", weight: 0.7 }
        - { address: "9f…",   weight: 0.3 }
      DONATION_MIN_TRANSFER: "0.1"
      DONATION_CREDIT_WALLETS:              # whose contributions we COUNT
        - { address: "9gGZ…", weight: 0.5 }
        - { address: "9aa…",  weight: 0.5 }
      DONATION_MIN_CONFIRMATIONS: 10
```

**`DONATION_WALLETS` — who we fund.** A prospective bet. It costs money, so the list is
short. Weights are normalised and split what has accrued.

**`DONATION_CREDIT_WALLETS` — whose contributions we recognise.** A retrospective
judgement. Holding it is free, so the list is long and accumulates. It weighs *other*
peers' donations when this node routes work.

Read the second list as a trust decision, not a preference. **It is the more critical of
the two**: a bad pay list costs the operator who set it, a bad count list is paid for by
the whole network, because it hands peers credit for funding whoever is in it. A pull
request adding an address to the shipped default is an economic act and should be
reviewed as one.

`nodo donations` prints both lists, what has been paid, what is accrued, and which
addresses are in one list and not the other. The TUI's EARNINGS page shows the same.

## Paying: a share of earnings, accrued and then paid

When an incoming payment is proved and credited to a client, the node accrues

```
owed += payment × DONATION_PERCENTAGE
```

in the **smallest native unit of the asset that was paid** — nanoERG for ERG — and never
in MU. A debt is incurred at the rate of the moment it was incurred; kept in MU, a later
change to that asset's rate would retroactively reinterpret money already owed.

The fraction is kept. Two per cent of a small payment has one, and discarding it on
every payment would make the node's effective rate drift below the configured one,
always in the node's own favour.

Why accrue at all instead of paying per payment: a 2 % cut is routinely below Ergo's
minimum payable output (0.001 ERG) and the transaction fee would eat it whole. The
counter is a rounding buffer of minutes or hours, not a deferral — the periodic tick
pays as soon as a transaction is worth making:

```
owed  ≥  max(DONATION_MIN_TRANSFER, outputs × minimum_output + fee)
```

Three rules follow from that, and each is a deliberate choice:

- **The fee comes out of the donation, never on top of it.** What leaves the node is the
  percentage, fee included, so donating can never cost more than the figure configured.
- **A share that cannot go out stays owed.** When a wallet's cut falls below the minimum
  output — or its address does not parse — that cut is *not* redistributed to the other
  wallets. It waits. Redistributed, a wallet with a small weight would fall below the
  floor every single time and never be paid at all.
- **One tick, one transaction.** The debt is paid before the wallet's excess is swept to
  cold storage: the debt is owed, the sweep is discretionary.

Every donation paid is recorded in the `payments` table with `purpose = 'donation'`, its
transaction id and its destination, so `nodo tx_history` and the TUI can tell it apart
from a payment to a peer. With `general_flags.SIMULATE_PAYMENTS` on, the node accrues and
logs but broadcasts nothing.

## Counting: read from the chain, by every node, independently

Once an hour the node scans each address in its `DONATION_CREDIT_WALLETS` through the
explorer and stores what it finds. The attribution rules:

1. Only outputs paying **our counted addresses** count. Change outputs and anything else
   are irrelevant.
2. The donor is the address of the transaction's inputs, and **only when every input
   belongs to one address**. A mixed-input transaction has no single donor and is skipped
   rather than guessed at.
3. That address is mapped to a peer through the payment address the peer **announced**
   for that ledger. The link is economic, not cryptographic: announcing an address you do
   not control means giving your revenue away, and claiming another peer's address to
   steal its credit costs 100 % of your income in that currency.
4. `DONATION_MIN_CONFIRMATIONS` (10) confirmations are required. The mempool is never
   read.
5. A donation to an address that is not in our counted list does not exist as far as this
   node is concerned.

A donor that maps to no peer we know is stored anyway — the peer may be introduced later,
and its whole history is credited the moment it is.

The scan is incremental, resumes from a stored height per address, and is idempotent: a
re-read of the same transaction changes nothing. An unreachable explorer is
**undetermined**, not a verdict of "nobody donated": the cached rows stand, the cursor
does not move, and if there is no usable data at all then *every* candidate's credit is
zero — never some of them, which would silently favour whichever peers happen to be
cached. A routing decision never waits on a network read.

## What the credit is worth

The balancer ranks candidates by an effective cost in log space:

```
score(peer)  = −ln(cost_mu) + SOCIALIZATION_FACTOR · r̂ + DONATION_WEIGHT · d̂
score(local) = −ln(cost_mu) + LOCAL_BIAS              + DONATION_WEIGHT · d̂

r̂ = r / (|r| + REPUTATION_HALF_CREDIT)     ∈ (−1, 1)    sign-preserving
d̂ = C / (C + DONATION_HALF_CREDIT)         ∈ [ 0, 1)    bonus only
```

Because price enters as a logarithm, **each weight is the maximum equivalent discount**.
`DONATION_WEIGHT = 0.3` means the largest imaginable donor beats a price up to
`e^0.3 ≈ 35 %` higher, **and never more**. Nobody buys dominance; they buy a generous
tie-break. The bonus saturates by construction, so "donated an absurd amount" cannot run
away with the ranking.

`C` is the credit in MU:

```
C = Σ  amount_mu × wallet_weight × (1 + ln(1 + age_seconds / DONATION_AGE_SCALE))
```

- Each donation is converted to MU at its own ledger's rate, and summed. That is what MU
  is for.
- `wallet_weight` is normalised to sum 1 within the ledger. This is not cosmetic:
  unnormalised weights of 100 would multiply every credit by a hundred and saturate
  everybody.
- Age is in **seconds**, not blocks. An Ergo block is ~120 s and a Bitcoin block ~600 s;
  in blocks, the same old donation would weigh five times differently per chain.

| age | multiplier |
|---|---|
| now | 1.00 |
| 1 year | 1.69 |
| 2 years | 2.10 |
| 5 years | 2.79 |

### Why an old donation is worth *more*

Each node keeps its own counted list, so rewarding age makes "fund whoever you believe
will be recognised tomorrow" a bet that pays off when that developer enters everyone
else's list. It is a prediction market on legitimacy, and it breaks the loop where
everyone donates to whoever is already receiving donations.

### Retroactive activation is a feature

Credit is always computed with the counted list **as it stands now**, so adding a wallet
activates every historical donation to it at once, with its age multiplier already
accrued. A peer who donated three years ago to a then-unrecognised developer collects
three years of credit the moment we add that developer. The chain never changed; our list
did. This is what makes the age factor worth anything.

## `DONATION_WEIGHT` is the safety parameter

Two tensions are contained by the ceiling rather than removed:

- A donation never decays, so an early donor collects a growing advantage. At
  `DONATION_WEIGHT = 0.3` the veteran hits the cap and stays there, reachable by others,
  and always subordinate to price and reliability.
- A new node cannot have old donations. **A high `DONATION_WEIGHT` closes the network to
  newcomers.**
- The curve is concave, so ten nodes with 1 ERG donated collect more aggregate bonus than
  one node with 10 ERG. The brake is that each identity needs real capacity and its own
  reliability score, which cannot be bought — but if `DONATION_WEIGHT` is ever raised
  much, splitting identities becomes profitable.

Weights are non-negative only. There are no exclusion lists and no negative weights:
turning the count list into a punishment mechanism is what would make forking rational.

## Our own donations count for us too

`d̂(local)` is computed from this node's own donations, read off the chain with the same
counted list as everyone else's — our wallet is our identity, so it is the same code path
with no special case. One consequence to accept deliberately: **if your pay list backs
someone your own count list does not recognise, your node earns no credit for it and
slightly disfavours itself.** That is honest — if you do not count a contribution as
recognised, you should not bill yourself credit for it.

`LOCAL_BIAS` is separate, and is not a reputation this node awards itself: we hold no
evidence about ourselves. It defaults to `1.0`, which reproduces the delegation policy
`nodo` has always had — local tolerates a price up to `e¹ ≈ 2.7×` higher than a peer's.

## Privacy

Donations link a node's wallet to its activity publicly. This adds nothing new: a node's
`peer_id` is already its public key, and its payment address is already announced to
every peer.

## See also

- [`CONFIG.md`](CONFIG.md) — every key, including the `balancers:` block.
- [`PRICING.md`](PRICING.md) — MU, what one is worth, and how a quote is built.
- [`ERGO.md`](ERGO.md) — the single wallet, and the cold-wallet sweep donations no longer
  have anything to do with.
