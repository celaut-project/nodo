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
moves for a top-up of another child or a tunnel. A check on readings like these
penalizes honest peers (#565, #569).

What holds is the **statement** of the account (#570): every movement, with its kind and
its cause, signed by the peer (`GetBalanceStatement`). The movements of an account are a
chain: each one carries the hash of the one before it, and the signed head commits to all
of them. A peer that signed a statement cannot show other movements for the same seqs
later, and a reader that goes on from the last movement that it checked never has to
trust a summary of the ones that came before.

### Our account on a peer

This node reads the statement of its own account on each peer where it has a delegated
child, every `ledger.STATEMENT_READ_EVERY_TICKS` ticks. It reads from the last movement
that it checked (the *mark*), in pages that its balance on the peer pays for, up to
`ledger.MAX_STATEMENT_PAGES_PER_TICK` pages. Each page continues the chain: the next seq,
hanging from the hash of the mark, adding to its balance, and ending in the head that the
peer signed. A new client is a new account: its mark starts at the genesis. Where there
is no child, the account is read when its mark is older than a tenth of
`ledger.MIN_PEER_RETENTION_DAYS`, so a peer cannot hide recent movements as retention.
Nothing may take MU from the account but:

- the start of a child of ours, no more than what was sent for it (`child_start`, and the
  `child_bound` row that names the child once it exists);
- a top-up of a child of ours, no more than what this node sent for it with
  `ModifyDeposit` (it is written down before the peer is asked, and taken off as the
  statement shows it);
- a statement that we asked for. What the peer shows never exceeds what this node asked
  for, in number and in MU at the rates that the peer announces: each read is counted
  when it is asked, before the peer can charge it. A read that fails (a timeout) may
  have been charged all the same: it is counted at the rows that it asked for, and once
  a later statement shows its range, at the rows that the account held when it was asked.
  The rates are those of today, so a peer that raises its prices loosens the bound for
  the reads that it priced lower before.

When the balance that the peer reports in `Metrics` (free, and not signed) pays for no
movement, the node reads the head of the account, which is signed and costs only the fixed
part, and reads the movements with the balance that the head says. A peer cannot stop the
check by reporting a low balance.

### A child

For a child, this node checks the chain and nothing else. What the peer charges a child
is the business of whoever runs it: the father is charged what the child consumed, and
the peer's build, ticks and tunnels are not judged. See [`PRICING.md`](PRICING.md) for
how that charge is found.

### What costs a peer reputation

| What the peer did | Reason | Change of `r` |
|---|---|---|
| A statement that does not continue the chain, is not signed by it, or does not come at all from a peer that answers | `statement_inconsistent` | -1000 |
| A charge to our account that this node did not cause | `statement_overcharged` | -1000 |

The same scale as a service that does not hash to its id (`get_service_wrong_hash`): in
every case the peer signed or reported something false about money. A fault is scored
once for each peer, account, reason and movement where it shows, and that is kept in the
database (`statement_faults`): it is not scored again at the next tick or after a
restart, and a different fault, at another movement, is. A peer that keeps refusing to
give a statement shows no movement, and is scored once a day for as long as it refuses. After a statement that does not
continue the chain the mark goes to the head, and the movements up to there are not
checked.

A statement that does not continue the chain is any of these: the head is behind the
mark, or is dated far from the clock of this node; a movement does not hang from the one
before it, does not add up, or is dated after the statement; the first movement after the
mark does not hang from its hash; the peer holds movements and does not show them; or the
peer deleted movements after the mark before its retention allows it.

Some things are not faults, and score nothing:

- A peer that cannot be reached. That costs `peer_refresh_failed` instead.
- A read that our balance on the peer does not pay for. It is not asked for. A peer that
  refuses one that the balance does pay for contradicts the balance that it reports, and
  that is a fault.
- A peer that answers `UNIMPLEMENTED` for a child: it may have delegated the child on,
  and the child is then charged from its balance, as with no statement. For our own
  account it is a fault.
- A refusal for lack of balance when our balance, read again, no longer pays for what
  was asked: the balance fell between the two reads.
- A first read, or a read after a long time, where the peer has already deleted the
  oldest movements. It announces its retention (`retention_days`, never less than
  `ledger.MIN_PEER_RETENTION_DAYS` for this purpose), and what it deleted after that
  time is not checkable. A reader that stays up to date, and keeps what it reads, can
  check all of it.
- A `child_start` that this node asked for and gave up on (#578): the peer may have
  finished it after `START_SERVICE_ON_PEER_TIMEOUT`. Every start is written down before
  the peer is asked, and a `child_start` that no child of ours explains is matched with
  one of those: it must be dated within the wait of the attempt, on the clock of this
  node, take no more than the attempt sent, and one attempt pays for one movement.
  Anything else is a fault at once; nothing waits.
- A peer that rejects a start because the amount is too small. The amount was the choice
  of this node.

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
