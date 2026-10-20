# How a peer's score is computed

When this node has work to delegate it ranks the candidates and takes the best one. The
rank is a number, every term in it is a key in the `balancers:` block of `config.yaml`,
and this page works the whole thing through at the shipped defaults. Every key is listed
in [`CONFIG.md`](CONFIG.md#balancers); the donation economics are in
[`DONATIONS.md`](DONATIONS.md).

## The formula

```
score(peer)  = −ln(cost_mu) + SOCIALIZATION_FACTOR · r̂
                            + ONCHAIN_REPUTATION_WEIGHT · ô
                            + DONATION_WEIGHT · d̂

score(local) = −ln(cost_mu) + LOCAL_BIAS + DONATION_WEIGHT · d̂
```

Highest wins. Price enters as a logarithm, so **each weight is the largest price premium
that term can beat**: a candidate that maxes out a weight of `W` still wins against a
price up to `e^W` higher, and never more. `local` — this node running the work itself —
gets neither reputation term, because a node holds no evidence about itself and what the
chain says about it is what it published.

`r̂`, `ô` and `d̂` are all bounded. `r̂` and `ô` are in `(−1, 1)` and sign-preserving;
`d̂` is in `[0, 1)` and a bonus only.

## `r̂` — what we saw ourselves

```
r̂ = r / (|r| + REPUTATION_HALF_CREDIT)          REPUTATION_HALF_CREDIT: 50
```

`r` is this node's own event log for that peer: it went up when the peer answered our
calls and took our payments, down when it failed. Nobody can buy it — the only way to
raise it is to behave well towards us. It saturates, so a peer that behaved well ten
thousand times is not a hundred times the peer that behaved well a hundred times.

At `SOCIALIZATION_FACTOR: 2` this is by far the heaviest term, which is the intended
ordering: reliability we observed outranks anything anyone paid for.

## `ô` — what the ledgers say

```
ô = S / (|S| + ONCHAIN_REPUTATION_HALF_CREDIT)   ONCHAIN_REPUTATION_HALF_CREDIT: 5.0 ERG
S = Σ_p cred(p) · sign(v_p) · min(|v_p| · burned_erg(p), ONCHAIN_PUBLISHER_CAP)
                                                 ONCHAIN_PUBLISHER_CAP: 5.0 ERG
```

Other nodes publish opinions on Ergo's reputation contract, each backed by ERG burned
irrecoverably. `v_p` is the share of itself proof `p` staked on this peer, so the money
behind one opinion is `|v_p| × burned_erg(p)`.

`cred(p)` is what that money is worth **to us**: the cosine between the opinions `p`
publishes about peers and our own `r̂` for those same peers, clamped at zero
(`max(0, cos)`). A proof we share no ground with scores `0` and its burn buys nothing —
that is the default and the common case. A proof that vouches for a peer that failed us
is discredited by that vouch. Disagreement silences a proof, it never inverts it.

Known limitation, stated rather than fixed: this node publishes its own scores, so a
mirror can copy them and reach `cred ≈ 1` for the price of the burn. The burn is then the
Sybil cost — real ERG, per subject — and a mirror only earns credibility with the peers
whose opinions it copied. `ONCHAIN_PUBLISHER_CAP` does **not** bound it: minting proofs is
free, so a burn split across several proofs never meets the cap. The cap shapes the curve
for a single honest publisher.

## `d̂` — donation credit

```
d̂ = C / (C + DONATION_HALF_CREDIT)               DONATION_HALF_CREDIT: 5000000000 MU
C = Σ  amount_mu × wallet_weight × (1 + ln(1 + age / DONATION_AGE_SCALE))
                                                 DONATION_AGE_SCALE: 31536000 (1 year)
```

What a peer paid to the wallets *this* node lists in `DONATION_CREDIT_WALLETS`, read off
the chain. A bonus only, never a penalty. Age increases the weight — 1.00 now, 1.69 at a
year, 2.79 at five — so funding a developer before everybody recognises them pays off when
they enter everybody else's list.

With `MU_PER_NANOERG: 1` the half credit is 5 ERG donated.

## The defaults, priced

`ONCHAIN_REPUTATION_WEIGHT` and `DONATION_WEIGHT` are both `0.3`, and both half-credits
are 5 ERG. **So one ERG burned is worth one ERG donated, at every point on the curve** —
neither channel is the better buy until an operator decides it should be.

| ERG spent | `ô` or `d̂` | log-space bonus | price premium it beats |
|---|---|---|---|
| 1 | 0.167 | 0.0500 | 5.1 % |
| 2 | 0.286 | 0.0857 | 8.9 % |
| 5 (the half credit) | 0.500 | 0.1500 | 16.2 % |
| 10 | 0.667 | 0.2000 | 22.1 % |
| ∞ | → 1 | 0.3000 | 35.0 % |

The burn column assumes `cred = 1` (perfect agreement with us) and, for a single proof,
no more than `ONCHAIN_PUBLISHER_CAP = 5` ERG behind it; `cred < 1` scales the whole row
down, and a burn above 5 ERG behind **one** proof stops adding. For comparison, the local
term: a peer with `r = 50` earns `2 × 0.5 = 1.0`, a price premium of 172 %. One
successful paid delegation is `+10`.

If you raise one weight above the other, the node logs a `[BALANCERS]` warning when the
first ERG burned is worth more than the first ERG donated — comparing `W_o / H_o` against
`W_d / H_d`. It is a warning, not a refusal: the split is the operator's to choose.

## Where the numbers come from

- **`r̂`**: the `reputation_events` table, written as this node transacts.
- **`ô`**: the `onchain_opinions` table, refreshed hourly from an Ergo explorer. `cred(p)`
  is computed on that same tick and stored beside each row, so a peer that failed us is
  reflected in its vouchers' credibility at the next tick.
- **`d̂`**: the `donations` table, refreshed hourly from the same explorer.

**A routing decision does no network I/O at all.** Every term is a read of SQLite. If an
index has never been filled, that term is zero for *every* candidate — never for some of
them, which would rank peers on the shape of a failure.

## What a peer charges, from its statements

This node pays `initial_mu` for a start and sends the peer that amount less its own
commission (#563). The peer takes its build from the amount that it got, and must take
nothing else from the account of this node. While the child runs, the peer charges its
maintenance to the balance of the child.

A balance cannot check any of this. It is the sum of every movement on it, and two
readings of it cannot tell a wrong charge from the rest: our account on the peer also
moves for a top-up of another child or a tunnel, and the balance of a child also moves
for what it spends on its own dependencies. A check on readings like these penalizes
honest peers (#565, #569).

What holds is the **statement** of the account (#570): every movement, with its kind and
its cause, signed by the peer (`GetBalanceStatement`). This node reads the statement of
its own account on each peer where it has a delegated child, and the statement of each
child, at every maintenance tick and right after `StartService`. It checks two things,
from the last movement that it already checked:

1. **It adds up.** Each movement takes the balance to its `balance_after`, the seqs have
   no gap, the last one is the balance of the answer, and the signature is the peer's.
2. **Each movement follows its rule.** On a child: the start is what this node sent, the
   build and the commission of the peer are within its quote (`build × (1 + variance)`),
   a maintenance period is within the rates that the peer announces for the resources
   that it names, at `scarcity_max_multiplier`, and the periods do not add up to more
   time than passed. On our account: nothing takes MU from it but the start of a child
   of ours (no more than what was sent for it), a top-up of a child of ours, or a
   statement that we asked for.

| What the peer did | Reason | Change of `r` |
|---|---|---|
| A statement that does not add up, is not signed by it, or does not come at all from a peer that answers | `statement_inconsistent` | -1000 |
| A charge beyond its rule | `statement_overcharged` | -1000 |

The same scale as a service that does not hash to its id (`get_service_wrong_hash`): in
every case the peer signed or reported something false about money. Each fault is scored
once for each account and reason, not at every tick.

Some things are not faults, and score nothing:

- A peer that cannot be reached. That costs `peer_refresh_failed` instead.
- A peer that refuses a statement while our account there cannot pay for it.
- A statement that the peer cut at its limit (`truncated`) before the movement after the
  mark. The span was too long to ask for. The tick goes on from the end of it.
- A `child_start` with no token, or with a token that this node has not stored yet. The
  start may be in progress, so the check stops there and looks again at the next tick.
  After `START_SERVICE_ON_PEER_TIMEOUT` and two ticks, it is a start that this node did
  not ask for.
- A child that spends on its own dependencies, tunnels or statements. The movement says
  so, and it is the child's to spend.
- A peer that rejects a start because the amount is too small. The amount was the choice
  of this node.

When a statement cannot be used, the tick charges the father the fall of the balance of
the child since the last reading.

## Why an instance died, and what it costs the service

When an instance of this node dies, the reason goes in `death_reason`. Only some reasons
lower the reputation of the service that the instance ran (#562).

The father holds the balance of the instance. He can empty it on purpose with small
top-ups (see "Limit what an instance can spend" in [`CONCEPTS.md`](CONCEPTS.md)). A death
that the father can cause is not a fault of the service. A death that the service causes
is a fault.

| Death | Reason in `death_reason` | Service penalty |
|---|---|---|
| The virtual machine does not exist any more | `process_lost` | `instance_lost`, -100 |
| The guest kernel panicked | `guest_panic` | `instance_lost`, -100 |
| The balance is empty (local or delegated instance) | `out_of_balance` | None |

The reason `instance_out_of_balance` is retired. The node does not write it. Old rows of
`reputation_events` can still hold it. Each instance is scored once for each reason.

## See also

- [`CONFIG.md`](CONFIG.md#balancers) — every `balancers:` key and its default.
- [`DONATIONS.md`](DONATIONS.md) — why the donation term exists and what bounds it.
- [`PRICING.md`](PRICING.md) — what `cost_mu` is.
- [`ERGO.md`](ERGO.md) — the reputation contract and what burning means.
