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

## See also

- [`CONFIG.md`](CONFIG.md#balancers) — every `balancers:` key and its default.
- [`DONATIONS.md`](DONATIONS.md) — why the donation term exists and what bounds it.
- [`PRICING.md`](PRICING.md) — what `cost_mu` is.
- [`ERGO.md`](ERGO.md) — the reputation contract and what burning means.
