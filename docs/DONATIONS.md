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

Three things, because `nodo` has two different reputations and they are not
interchangeable:

| | earned by | purchasable | in the balancer |
|---|---|---|---|
| **Local reputation** (`peer.reputation_score`) | behaving well towards *this* node — taking our payments, answering `GetPeerInfo` | no — the only way to raise it is to behave well towards us | bonus *and* penalty, at `SOCIALIZATION_FACTOR` (`2`) |
| **On-chain reputation** (opinions on Ergo's reputation contract) | other proofs staking part of themselves on you, backed by irrecoverably burned ERG | **yes, by burning — that is what the burn is** | bonus *and* penalty, at `ONCHAIN_REPUTATION_WEIGHT` (`0.1`), and **only after each proof's burn is discounted by how far that proof agrees with what we have seen ourselves, then capped** |
| **Donation credit** | contributing money, verifiably on-chain | yes, that is the point | bonus only, at `DONATION_WEIGHT` (`0.3`) |

### What actually protects the routing decision

Not "reputation is not purchasable" — that was only ever true of the first row. Three
different things protect the three terms, and they are worth naming separately:

- **The local term is locally observed.** `compute_reputation` is our own event log and
  nothing else (`src/reputation_system/interface.py`). Nobody can pay to move it; they
  can only take our payments and answer our calls.
- **The donation term is bought by design**, and that is fine, because it is bounded
  (`d̂ ∈ [0, 1)`), it is a bonus only, and what it buys is the funding of the software
  every node here runs.
- **The on-chain term is bought by design too** — an opinion is worth `share × burned
  ERG`, minting a proof is free, and the ERG put into one can never come back out, which
  is the ecosystem's only Sybil resistance (see [`ERGO.md`](ERGO.md)). The burn is
  counted, at its own weight, **after being repriced by us**. A reputation proof is not a
  peer: a peer is a node we have transacted with and keep an event log about, a proof is
  a token that publishes opinions. We keep local reputation on peers only, and work out
  what a *proof* is worth by comparing the opinions it publishes about peers against our
  own scores for those same peers — the cosine over the peers both of us rate. Its burn
  is scaled by that, capped per proof, then saturated.

  So a proof that vouches loudly for a peer that failed us is discredited **by that
  vouch** rather than by a rule about who owns it; a proof we share no ground with scores
  zero however much was burned into it, which is the default and closes the
  mint-a-second-proof trick; and a newcomer can still earn a voice by agreeing with us
  about peers we both know, which an owner test could never allow.

The traffic used to be one-way: `submit_to_ledger` publishes our local scores as staked
opinions and nothing was read back. It is now a loop, but a lossy one on purpose — what
comes back is priced by us, not by whoever spent the most.

### Why the weight, not the shape, is the safety parameter

**Read `ONCHAIN_REPUTATION_WEIGHT / DONATION_WEIGHT` as the exchange rate between
destroying one ERG and donating one.** At the shipped `0.1 / 0.3`, a donated ERG is worth
three times a burned one, and the node **refuses a config where the burn weighs more**
(`validate_balancers_config`).

That inequality has to be deliberate, because every other advantage already sits on the
burn's side:

- **Universal recognition.** One contract, one filter, every node reads the same boxes.
  Donation credit only counts for nodes whose `DONATION_CREDIT_WALLETS` lists the wallet
  you funded — recognition rate ρ < 1, and ρ ≈ 0 for a developer nobody lists yet.
- **Instant.** No `DONATION_AGE_SCALE` to wait out (1.00 now, 1.69 at a year).
- **Sign-preserving**, not bonus-only.

Reusing `SOCIALIZATION_FACTOR = 2` for the import would have handed the burn 6.7× the
log-space bonus of a donation on top of all that, and a rational operator would have
stopped donating and burned instead — destroying the money rather than funding the
development donations exist to pay for. That is the substitution issue #353 records, and
the separate weight is what closes it.

Filtering out a node's opinion about itself does **not** close it, and is not claimed to:
a second proof costs nothing to mint, nothing on-chain ties it to its owner, and R7 is a
wallet — wallets are free too. `split_own` is hygiene for the report (issue #351). The
defence is that a proof nobody has ever heard speak has agreed with us about nothing, so
its burn buys nothing.

**And agreement can be mirrored — this is the term's known weakness, not a detail.** This
node publishes its own local scores to the chain (`submit_to_ledger`), so anyone can read
them, mint a proof, restate them verbatim and reach near-perfect agreement for the price
of the burn. Dating the boxes does not help: revising a box spends it and rewrites its
date. What contains it is the bounding, which is why the weight and not the shape is the
safety parameter — a perfect mirror is still capped at one proof's worth, still saturates,
and still sits under `DONATION_WEIGHT`, so the most it can buy is the term's ceiling,
priced below 3 ERG donated. A mirror also has to keep mirroring, publishing that the peers
we distrust are untrustworthy, which is not free for a coalition to say.

What this shape does **not** cost is bootstrapping. A rater unknown to us is not mute by
decree: it is worth what it agrees with us about, so a newcomer with no history of
transacting with us can still be heard. What stays true is that agreeing about nothing is
worth nothing — `LOCAL_BIAS` and the donation term are what a node with no overlap has.

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
short. Weights are normalised, and each wallet's weight is its share of everything this
node has ever earned — not of whatever transaction happens to be going out, which is a
different and much weaker promise (see "Paying" below).

**`DONATION_CREDIT_WALLETS` — whose contributions we recognise.** A retrospective
judgement. Holding it is free, so the list is long and accumulates. It weighs *other*
peers' donations when this node routes work.

Read the second list as a trust decision, not a preference. **It is the more critical of
the two**: a bad pay list costs the operator who set it, a bad count list is paid for by
the whole network, because it hands peers credit for funding whoever is in it. A pull
request adding an address to the shipped default is an economic act and should be
reviewed as one.

`nodo donations` prints both lists, what has reached **each** funded wallet, what is
accrued, and which addresses are in one list and not the other. The per-wallet figure is
there so a weight can be checked rather than trusted: a wallet given 0.1 % should be able
to show that something arrived. The TUI's EARNINGS page shows the same, per wallet.

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
what would leave the node  ≥  DONATION_MIN_TRANSFER
each output                ≥  the chain's minimum output, after bearing its share of the fee
```

Three rules follow from that, and each is a deliberate choice:

- **The fee comes out of the donation, never on top of it.** What leaves the node is the
  percentage, fee included, so donating can never cost more than the figure configured.
  The fee is borne by the wallets in *that* transaction, in proportion to what each
  receives — never by weight across the whole list, which would bill a wallet too small
  to be paid for every transaction it was not in.
- **A share that cannot go out stays owed *to the wallet that earned it*.** When a
  wallet's cut falls below the minimum output — or its address does not parse — that cut
  is *not* redistributed to the other wallets, and it does not go back into a pool
  either. It stays attributed.

  This is the part that took two attempts. A payout is computed from what each wallet is
  still owed, not from what the debt happens to be now:

  ```
  entitlement(wallet) = weight(wallet) × (everything ever accrued) − (already paid to it)
  ```

  The first version recomputed each cut from the live debt and returned what it could not
  pay to a debt belonging to nobody. One tick later that money was split among everybody
  again — so a wallet with a weight of 0.001 never cleared the floor, never got paid, and
  its share ended up in the big wallets after all. The rule was in this document and in
  every docstring; the tests agreed with the rule because each of them drove a single
  payout, and the property is about a *sequence* of them. `donation_payouts` is the table
  that remembers whose money is waiting, and `nodo donations` prints what has reached
  each wallet so the claim above can be checked rather than trusted.

  It holds for the unparseable address too: its entitlement simply accumulates and is
  paid in full the moment the address is corrected.
- **The minimum transfer bounds what leaves, not what is owed.** `DONATION_MIN_TRANSFER`
  says "never make a transfer smaller than this". Compared against the debt it would let
  a node with a 2 ERG minimum broadcast a transaction moving half of that — the two
  figures differ by exactly what is being withheld — and pay a full fee for it.
- **One tick, one transaction.** The debt is paid before the wallet's excess is swept to
  cold storage: the debt is owed, the sweep is discretionary.

### Several assets, one transaction

A debt is per payment **method** — `(ledger, contract, asset)` — so a node paid in ERG
and in a token owes two independent debts, each in its own base unit, and a debt in one
cannot be paid out of the other. They are still paid **together**: one Ergo output
carries several assets, so a payout per asset would pay a fee per asset, and each of
those fees is money donated on top of the configured share. Each donation wallet gets one
box carrying everything it is owed, and every debt is decremented in a single database
transaction — the window between two commits is exactly where a crash pays one asset's
debt twice.

The fee is where the asymmetry shows. An Ergo fee is paid in ERG, and a debt in SigUSD
cannot pay it. So ERG's debt declares the fee — which is what keeps the promise above
true for ERG — and a token's declares none, meaning **a token donation costs the node
ERG it never accrued**: the fee, plus a carrier box for any wallet owed only tokens. The
node says so on the log line rather than letting that ERG leave silently, and a wallet
with no ERG cannot pay a token donation at all (see [`ERGO.md`](ERGO.md), "Native
tokens"). `DONATION_PERCENTAGE` and `DONATION_MIN_TRANSFER` are per asset, inside each
`ASSETS` entry; the two wallet lists are not, because an Ergo address receives anything.

Every donation paid is recorded in the `payments` table with `purpose = 'donation'`, its
transaction id, its destination and the `token_id` it was paid in. `purpose` is what
tells a donation apart from a payment to a peer — that is what `nodo tx_history` and the
TUI read — and `token_id` is what says which money it was: `amount_mu` is deliberately
ledger-neutral, so without the asset two rows of one tick paying two assets would be
indistinguishable. `nodo tx_history` prints it beside the purpose. With `general_flags.SIMULATE_PAYMENTS` on, the node accrues and
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
  *locally observed* reliability score, which cannot be bought — but if `DONATION_WEIGHT`
  is ever raised much, splitting identities becomes profitable.

At `0.3` the ceiling is deliberately modest against the term beside it. With
`MU_PER_NANOERG: 1` the half credit is 5 ERG, so donating it earns `0.15`, and the whole
ceiling is `0.30` — while one successful paid delegation (`+10`, `PAYMENT_COMMUNICATED`)
is already worth `0.33`. Donating buys a tie-break; being a peer worth delegating to is
worth more, which is the intended ordering.

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
