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
build (#563, #577). The peer takes its build from the amount that it got, and must take
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
While the mark is that old, a read is due every `ledger.STATEMENT_READ_EVERY_TICKS`
ticks, as with a child. A read that fails does not make the next tick read again.
Nothing may take MU from the account but:

- the start of a child of ours, no more than what was sent for it, and once (`child_start`,
  and the `child_bound` row that names the child once it exists). Each start that this
  node asks for goes with a `StartReference` of its own (#578): a nonce and a MAC of
  the nonce with a key of this node. The peer writes the reference in
  `detail.start_ref` of the `child_start`, so this node knows which of its requests
  caused each start, with no matching by amount or time, and also after a restart. A
  `child_start` with no reference, or with a reference that has no valid MAC, is a
  start that this node did not ask for: a fault at once;
- a top-up of a child of ours, no more than what this node sent for it with
  `ModifyDeposit`. Each top-up is written down before the peer is asked
  (`top_up_reservations`), and the statement takes from it as it shows the movement.
  The movement must be dated near the time of the top-up: no later than
  `communication.SIMPLE_RPC_TIMEOUT_SECONDS` and two ticks after it. When the peer
  refuses the top-up, the reservation is deleted. When the answer is lost, the
  reservation stays until its time ends, because the peer can have done the top-up;
- a statement that we asked for. What the peer shows never exceeds what this node asked
  for, in number and in MU at the rates that the peer announces: each read is counted
  when it is asked, before the peer can charge it. A read that fails (a timeout) may
  have been charged all the same. For our own account, the peer writes the charge of a
  read in the same transaction as the read. The charge names the range of the read. So a
  failed read counts for the rows of its range before its charge, by seq. Until
  its charge shows, it counts for 0, and when the head shows no charge long after the
  read, it stays at 0. For a child, a failed read counts for the rows that it asked
  for, and a second read of the same range counts for 0. When the statements of the
  child show the range, over one page or more, it counts for the rows dated before it.
  The rates are those of today, so a peer that raises its prices loosens the bound for
  the reads that it priced lower before.

The balance that the peer reports in `Metrics` is free, and not signed. So a read uses
the larger of that balance and the *signed floor*. The signed floor is the balance of the
mark, which the peer signed, less all that this node caused since. That is the starts
that hold no movement yet, the top-ups that are reserved, and the statements whose charge
did not show yet. A peer cannot stop the check by reporting a low balance. A refusal for
lack of balance is a fault when the signed floor pays for the read. When neither balance
pays for a movement, the node reads the head of the account. The head is signed and costs
only the fixed part. The node then reads the movements with the balance that the head
says.

### A child

For a child, this node checks the chain, and the maintenance that the peer charges the
child against the quote that the peer gave for it (#567). The father is charged the price
of this node (#577), so what the peer charges is the cost of the delegation, and the
margin of this node is the difference. The build of the peer and its tunnels are not
judged. See [`PRICING.md`](PRICING.md) for how the charges are found.

**The maintenance against the quote.** When this node delegates a start, it keeps the
maintenance quote of the peer on the delegated row (`quote_*`): `init_maintenance_cost`,
`max_maintenance_cost`, `maintenance_seconds_loop` and `variance` of its
`EstimatedCost`, in the MU of the peer, not converted. Each statement of the child that
continues the mark is an interval:

- Its cost is the sum of its `maintenance` movements. Its seconds are the sum of the
  `seconds` that each of them priced, but no more than the time from the mark to the last
  of them, one period, and `CLOCK_SKEW_SECONDS` (120 s) for the difference between the
  clocks of the two nodes. So the time that this node could not read the peer is not in
  the interval, and a peer cannot lower its price per second with seconds that did not
  pass, beyond that 120 s margin.
- The interval charges too much when its cost per second is more than
  `max_maintenance_cost / maintenance_seconds_loop * (1 + variance)`, with 1 MU for each
  movement for the rounding. The comparison is on the scale of the peer, before any
  conversion.
- `max_maintenance_cost` is the price of the most resources of the service, so a hotplug
  up to them is not an overcharge.
- The quote is the one that the peer signed for this instance. A peer that raises its
  prices after the delegation is held to the price that it signed, not to its rates of
  today.
- A row with no quote, or a quote with a `max_maintenance_cost` of 0 (a service with no
  `at_most`) or no period, is not checked.

A peer that charges more loses `maintenance_overcharged`. The signed quote is the proof.
Only the peer is scored: not the service and not the client. The instance is not moved
or stopped, and its charge to the client does not change: this node chose to delegate,
so it absorbs the excess (#577).

### What costs a peer reputation

| What the peer did | Reason | Change of `r` |
|---|---|---|
| A statement that does not continue the chain, is not signed by it, or does not come at all from a peer that answers | `statement_inconsistent` | -1000 |
| A charge to our account that this node did not cause | `statement_overcharged` | -1000 |
| More maintenance on a child than the quote that it gave for it | `maintenance_overcharged` | -1000 |
| A signed `stop` row of a descendant with MU left, and the father gone ([#631](#who-records-the-fault)) | `killed_with_mu` | -1000 |
| Maintenance of a descendant after a tunnel to it failed ([#631](#liveness-the-ancestor-tunnel)) | `maintenance_while_unreachable` | -1000 |

The same scale as a service that does not hash to its id (`get_service_wrong_hash`): in
every case the peer signed or reported something false about money. A fault is scored
once for each peer, account, reason and movement where it shows, and that is kept in the
database (`statement_faults`): it is not scored again at the next tick or after a
restart, and a different fault, at another movement, is. A peer that keeps refusing to
give a statement shows no movement. It is scored at most once in 24 hours for as long
as it refuses. The 24 hours start when it is scored, not at midnight UTC. After a
statement that does not continue the chain the mark goes to the head, and the movements
up to there are not checked. A statement dated far from the clock of this node is
different: the clock that is wrong can be ours. It is scored at most once in 24 hours,
and the mark stays where it is. A price above the quote shows at every period, for as
long as the peer keeps it. So `maintenance_overcharged` is scored at most once in 24
hours for each child, at the first movement of the interval, and a movement is never
scored twice. `maintenance_while_unreachable` is scored once for each peer and
descendant, at any movement: one dead VM costs the peer one fault.

A statement that does not continue the chain is any of these: the head is behind the
mark, or is dated far from the clock of this node; a movement does not hang from the one
before it, does not add up, or is dated after the statement; the first movement after the
mark does not hang from its hash; the peer holds movements and does not show them; or the
peer deleted movements after the mark before its retention allows it. A movement dated
before the one before it, or before the mark, is not a fault: the clock of an honest
peer can step back. The node uses the date before it for that movement.

Some things are not faults, and score nothing:

- A peer that cannot be reached. That costs `peer_refresh_failed` instead. But a peer
  selects the code that it answers with. A timeout or `UNAVAILABLE` of the statement is
  a refusal, and a fault, when two conditions are true. The peer answers `GetMetrics` in
  the same tick. The mark is older than half of `ledger.MIN_PEER_RETENTION_DAYS`.
- A read that our balance on the peer does not pay for. It is not asked for. A peer that
  refuses one that the balance does pay for contradicts the balance that it reports, and
  that is a fault.
- A peer that answers `UNIMPLEMENTED` for a child: it may have delegated the child on,
  and the child is then charged from its balance, as with no statement. For our own
  account it is a fault.
- A refusal for lack of balance when our balance, read again, no longer pays for what
  was asked, and the signed floor does not pay for it either: the balance fell between
  the two reads.
- A first read, or a read after a long time, where the peer has already deleted the
  oldest movements. It announces its retention (`retention_days`; 0 is for ever, and
  this node reads it as never less than `ledger.MIN_PEER_RETENTION_DAYS`, so a peer that
  announces 0 and deletes after that time is not caught), and what it deleted after that
  time is not checkable. A reader that stays up to date, and keeps what it reads, can
  check all of it.
- A `child_start` that this node asked for and gave up on (#578): the peer may have
  finished it after `START_SERVICE_ON_PEER_TIMEOUT`. The peer charges a start for each
  candidate that it tries, with the same reference, and it gives back the start of each
  one that fails: a `refund` of a movement sets its reference free again. One reference
  holds one movement that is not given back. A `child_bound` that names a child of ours
  closes the reference, and a later `child_start` with it is a fault. A start that this
  node gave up on has a child that nobody holds: the father got an error and its MU
  back. After the timeout, the node asks the peer again with the same reference and a
  short deadline, and stops the child that it gets. If that fails too, the next check of
  the account that shows the `child_start` asks again, at each check, until it gets the
  child and stops it. The peer then gives back what the child did not use, as a `refund`
  of the `child_start`. While the node asks, the movement is not a fault.
- A start that the peer refused. The peer must give back the start of each candidate
  that it tried before it answers. A movement of a refused start that is not given back
  by the head of a later read is a fault.
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

## Read the statements of descendants

A node can kill an instance that still has MU and keep the MU. While the instance has a
parent, the parent can judge the node with the signed statements (#570). If the parent
is dead, the instance has no judge. This section closes that gap (#631).

### The link, the stop and the read

- **Link.** A `child_start` pays for a child before the child exists. The `child_bound`
  row that follows names the child by its `instance_ref`:
  `blake2b("celaut-ledger-instance:" + token)`. The `instance_ref` is a one-way hash. It
  names the account and gives no access to the instance. A statement never holds a token.
  For a delegated child, the hash is of the alias that this node gave, not of the peer
  token. `detail.service_id` of the row is the service of the child (the hex of its
  content hash), so a reader knows which API each descendant has. Old rows do not have
  it.
- **Stop.** The chain of an instance ends with a `stop` row. The row moves nothing.
  `detail.reason` is the death reason. `detail.leftover` is the MU that the `refund`
  took out of the account. If the node kills an instance that has MU, the last row of
  its signed chain shows it.
- **Read.** `GetBalanceStatement` has a request field `instance_ref`. The caller shows
  a token that it already holds, or none for its own account. The node looks for the
  `instance_ref` below that account, one level at a time, through the `child_bound` rows
  of each chain. It never accepts an `instance_ref` that no chain links. The caller does
  not need the token of the descendant. The node answers with the statement of the
  descendant, signed by the node. The statement can be of an instance that stopped.

A grandparent reads a child with `token` = the parent and `instance_ref` = the child.
The parent can be dead: its row and its chain stay while anything below it lives, at any
depth, and after that for `ledger.RETENTION_DAYS` (0 is for ever, the default). The
`instance_ref` of the instance of `token` itself is also accepted: it reads that account,
also when it stopped.

The statement of a descendant also gives `service_id`: the service that the descendant
runs, as the lowercase hexadecimal of its content hash. The node takes it from its row of
the instance. Each `child_bound` row also holds it in `detail.service_id`. The field is
empty when the node does not know the service, and in the answer to a read without
`instance_ref`. The digest covers `service_id` only when it is not empty
(`|service_id:` and the hex of its UTF-8, after the movements and before `|relayed`).

The statement of a descendant also gives the `instance_ref` of the request. The node signs
that the statement is the account of that descendant, so it cannot say later that it was
the statement of another account. A reader refuses an answer to a read with
`instance_ref` that does not have the same `instance_ref`. The digest covers it only when
it is not empty (`|instance_ref:` and the hex of its UTF-8, after `service_id` and
before `|relayed`).

### Delegated children

The node has no chain below an instance that it delegated. The peer has it. The node
asks the peer with its own client_id and with the token that the peer gave it for that
instance, one hop at a time. The service never sees a peer id or a peer token.

- For a descendant that is itself the delegated instance, the node sends the
  `instance_ref` of the token of the peer. The peer reads its own account, also when the
  instance stopped.
- For a descendant below a delegated instance, the node sends the same `instance_ref`.
- When the peer gives no `service_id` for the delegated instance, the node sets the
  service that it asked the peer to run, before it signs the statement.

The node checks the signature of the peer with the key of the peer, and that the peer
signed the `instance_ref` that the node sent. Then it sets `relayed` and the
`instance_ref` that its own caller sent on the statement, and signs it with its own key. The digest covers `relayed`
only when it is true (`|relayed` at the end of the text), so the digest of a statement
that a node wrote itself does not change. A node above checks the statement with the
key of the node that it asked, so a read across two or more delegations works. The rows
and their hashes do not change. The reader trusts its own node, as it does for every
other answer.

No error that goes to the caller names a peer, an address or a token. The details stay
in the log of the node.

### Cost and limits

- For a local descendant, the caller pays as for any statement: a fixed part and a part
  for each row, as a `statement_query` row that names the descendant by its
  `instance_ref`. The node writes the charge in the same transaction as the read.
- For a read that goes to peers, **the instance that is called pays**. For each peer that
  the node asks, the delegated instance that this node holds the account of (the alias:
  the descendant itself when it is the delegated one, else the delegated instance that
  leads to it) pays one read, with a `statement_query` row that names the descendant,
  before the first call to a peer. A read is the fixed part and the part for the most rows
  that the read can return (`balance_statement.read_price_mu`). The caller pays nothing
  for these. The fan-out below each peer is paid by that peer in the same way, from the
  accounts that it holds.
- A delegated instance that cannot pay (it stopped, or it has no balance) leaves its read
  to the caller: one read for each such peer, charged once, before the first call. This
  is the case of a stopped orphan, whose last statement is the reason for the read.
- A node asks a peer only for the delegated descendant itself, or when that peer signed a
  `child_bound` row of the `instance_ref` in a statement that it gave, since
  `ledger.link_horizon` (no limit by default, else `ledger.RETENTION_DAYS` ago;
  `SQLConnection.descendant_linked`). The duty of a peer to answer for a descendant that it
  must show is held to `ledger.MIN_PEER_RETENTION_DAYS`, the retention that this node can
  ask of every peer. A ref that no peer linked is refused with no
  call, and nobody pays. A caller that asks for refs that it made up costs the node
  nothing. To reach a descendant below a delegated instance, read the statement of the
  instance above it first: its `child_bound` rows name the ref and the node keeps them as
  links.
- This node counts the most that a peer may charge it for a read
  (`statement_check.descendant_read_counts`) for its check of the charges on the peer. The
  peer charges at most that.
- The walk goes at most `ledger.MAX_DESCENDANT_DEPTH` levels (8) and looks at
  `ledger.MAX_DESCENDANT_ACCOUNTS` accounts (200). It looks at each account once, so a
  loop ends it. These limits apply to the chains of one node.
- One answer holds at most `ledger.MAX_STATEMENT_ROWS` rows.

### Hop budget

The limits of the walk do not count the hops between nodes. The read uses the hop budget
of the `RecursionGuard` ([`RECURSION_GUARD.md`](RECURSION_GUARD.md)) for them.
`BalanceStatementRequest` has the field `remaining_hops`.

- A node that gets no `remaining_hops` uses `network.RECURSION_MAX_HOPS`. A value above
  that maximum becomes the maximum.
- A node that asks one peer sends `remaining_hops` minus 1.
- A node that asks more than one peer shares `remaining_hops` minus 1 between them: it
  asks at most that number of peers, and each one gets an equal part
  (`balance_statement.split_hops`). So one read never reaches more nodes than its
  budget, also when each node has many delegated descendants. Without this, the work
  grows as the number of peers to the power of the hops.
- A node that gets 0 refuses. A node that gets 1 answers from its own chains and asks no
  peer. In both cases the caller gets UNIMPLEMENTED, and the read does not continue.
- The read has no token. A loop through peers ends with the budget. Each node charges
  the instance that is called for the peers that it asks before it asks them, so the
  total cost is bounded as well.

### Two peers for one descendant

A peer can sign a statement for a ref that it does not hold, once it linked that ref. When
more than one peer gives a statement of the same descendant, the node chooses the peer
that linked the ref first (`descendant_peers.first_linked_ts`,
`balance_statement.first_linked`): a ref cannot be linked before the chain that holds it
has shown it. The tunnel does the same. If the dates are the same, or one peer has none,
the node gives no statement (`More than one node below gave a statement of that
descendant`) and no tunnel.

### Threat model

A malicious node A runs an instance C. The parent P of C is dead. C has MU.

- A kills C and keeps the MU. A signs the `stop` row, or A refuses to show the
  statement. The `stop` row shows the reason and the MU that stayed. A refusal is
  `statement_inconsistent`.
- A live ancestor G of P asks its own node for the descendants of P. G needs no token
  of C and no peer id.

Cases that this does **not** cover:

- Every ancestor up to the root client is dead. Nobody asks.
- The ancestors ask after the retention ended. With `ledger.RETENTION_DAYS` above 0, the
  node deletes the chain of a dead instance with nothing alive below it that many days
  after its stop. The default (0) never deletes it.
- The node that links P to C deleted the `child_bound` row (only with a
  `ledger.RETENTION_DAYS` above 0, and only when P and everything below it are dead), or P
  is gone from that node. The link is lost.
- A peer that writes a false `stop` row with no leftover. The ancestor tunnel covers a
  part of this case (see below). The reason of the row does not matter for
  `killed_with_mu`: see "Known limits".
- A peer that writes that the father got the refund. The statement of the father shows
  whether it did: an ancestor can read it.
- A node that signs a false `stop` row. The row is signed, so it proves the node lied
  only if another signed source disagrees.
- The node of the reader and the malicious node are the same node.

### Who records the fault

The node of the reader records the fault by itself. The service that reads sees only the
statement and never a peer id. The service does not decide and does not report.

The node judges each statement that a peer gives, also a `relayed` one: the peer answers
for the nodes that it delegates to. The node below the peer judges its own peer too.

When the node asks a peer for a descendant, it reads the `stop` rows of the signed
statement. The node scores the peer with the reason `killed_with_mu` (-1000, once for each
descendant and row) if all these conditions are true:

- `detail.leftover` of the `stop` row is more than 0.
- The row before the `stop` row is the `refund` of the leftover with
  `detail.lost = father_gone`. The father of the descendant was gone, nobody could judge
  the peer, and the MU stayed with it. A father that stops its child gets the leftover:
  that stop is not scored.
- The reason does not matter. An orphan ends only when its MU end, so a stop with MU left
  is the fault of the node that ran it, also a machine that failed (`process_lost`,
  `guest_panic`) or a launch that failed.
- For `out_of_balance`: the leftover is at least the price of the tick that the instance
  could not pay. The `stop` row gives it (`detail.tick`). A `tick` above 10 times the
  largest maintenance row of the statement is not believed: that limit is used. With no
  `tick`, the largest maintenance row is used. Less than one tick is the normal end of an
  instance that cannot pay. With neither, the node does not judge.

A statement that does not show the `refund` row is not judged. A local descendant is not
scored: this node ran it.

Open points:

- The fault needs a stop row that this node reads. If nobody reads, the node does not
  score. The read of a descendant is the trigger.
- Old chains have no `stop` row. Only new stops have one.

### Liveness: the ancestor tunnel

A statement shows money, not life. A node can kill the VM and sign a tail of maintenance
that agrees with the quote. Only a user of the API of the instance can see that it is
alive. So any ancestor can open a tunnel to a descendant and use its API: to fake an
answer, the node must run the service.

`ServiceTunnel` takes a `DescendantTunnel` at index 2 in place of the `TokenMessage`:
the token of the ancestor, the `instance_ref` of the descendant, the slot, `receipts` and
`remaining_hops`. The node finds the descendant as the read does
([`TUNNELING.md`](TUNNELING.md#descendants)).

| Case | Path of the tunnel |
|---|---|
| G, P and C on the same node A | G to A with (token of P, ref of C). A checks the link and opens the tunnel to C. |
| G on X; X delegated P to A; C local on A | G to X with the alias of P. X to A with its own token of P and the ref of C. X relays. |
| P local on A; A delegated C to B | G to A. A to B with its own token of C. A relays. |
| A chain of delegations | Relay hop by hop. `remaining_hops` limits it. |

G always got the token of P from its own gateway. So G never holds a token that its node
does not know.

- A local descendant that is alive: the node opens the tunnel. The descendant pays the
  traffic.
- A descendant on a peer: the node relays the bytes, one hop at a time, each hop with the
  token that it holds there. Each node meters its own leg: against the alias of the
  descendant when it holds it, else only against the ceilings of the operator.
- The receipts name the execution by the `instance_ref`
  ([`EXECUTION_RECEIPTS.md`](EXECUTION_RECEIPTS.md#the-execution-name)). Each node signs its
  own leg. A node does not pass on the receipts of the next node.
- The ancestor gets only the tunnel. To stop the instance or to change its deposit
  still needs the client (#597).

The node writes the result of each hop to a peer, for the peer and the `instance_ref`
that the hop sent (`descendant_peers`): the time of the last hop that relayed a byte,
and the times of the first and of the last hop after it that failed with the descendant
unavailable (`UNAVAILABLE`,
no answer, or no connection). A refusal (`PERMISSION_DENIED`, `RESOURCE_EXHAUSTED`,
`INVALID_ARGUMENT`) is not a failure.

When the node later reads a statement of that descendant from that peer, also a
`relayed` one, it scores the peer with `maintenance_while_unreachable` (-1000, once for
each peer and descendant) for a `maintenance` row that:

- charges a period between two failures, with no hop that worked after the first: the
  period starts more than `CLOCK_SKEW_SECONDS` after the first failure, and the row is
  dated more than `CLOCK_SKEW_SECONDS` before the last one. The period of a row starts
  `detail.seconds` before the date of the row. One failure is not enough: the instance
  can answer again after it. An honest node can charge the period in which the VM
  stopped, and write the `stop` row at the next tick: that row is not scored;
- is not after a `stop` row;
- charges memory (`detail.mem_limit` is not 0). A row of the disk of a hibernated
  instance has no VM to answer. When a row with no memory charges a period between the
  two failures, no row is scored: the failures can be of the hibernation.

If the statement shows a `stop` row with MU left instead, `killed_with_mu` applies.

What the node of the ancestor can see:

| The tunnel | The statement | Result |
|---|---|---|
| Works | Any | The instance is alive. The hop that worked cancels an earlier failure. |
| Fails twice, descendant unavailable | `maintenance` of a period between the failures | `maintenance_while_unreachable` |
| Fails, descendant unavailable | `stop` row with MU left, father gone | `killed_with_mu` |
| Fails, descendant unavailable | Only rows before the failure, or a `stop` row | No fault |
| Refused (no link, bad slot, no balance, no hop budget) | Any | No fault: the hop is not recorded |

This judgement is local to the node of the ancestor. A failed tunnel is not a signed,
portable proof. An invented tail of maintenance can only start after the last tunnel
that worked, and the node never knows when the next one comes.

Cases that the tunnel does **not** cover:

- Nobody opens a tunnel. The fault needs a failed hop and a later read of the statement.
- The peer refuses the hop with a refusal code (`PERMISSION_DENIED`,
  `RESOURCE_EXHAUSTED`, `INVALID_ARGUMENT`) in place of `UNAVAILABLE`. A refusal is not
  recorded, so a peer that lies about the code is not scored. The ancestor still gets no
  answer from the API, and can stop to trust that peer.
- A service that does not listen on its slot. The VM runs, but the tunnel fails as
  `UNAVAILABLE`. The node cannot tell this from a VM that is not there, so it can score
  a peer that charges for a VM that runs a broken service.
- A node below the peer fails. The peer passes `UNAVAILABLE` up, and the node scores the
  peer that it asked: that peer is answerable to it for the hops below.
- The node of the ancestor reads the statement after the retention ended, or it deletes
  its hop records (`descendant_peers`) with the retention. With the default retention (0)
  the node deletes neither.

## Known limits of this version

These are decisions for the alpha. Each one is a known limit, not a defect that is hidden.

- **An orphan ends only when its MU end.** Nobody can stop it by the API: `StopService`
  needs the token, and only the father holds it. The node can always stop it by itself
  (`nodo kill`, or an internal stop), and that is the fault: any `stop` with leftover above
  0 and `lost = father_gone` is scored as `killed_with_mu`, whatever reason the row gives.
  A VM that went down is started again, for as long as the instance has MU. A failure of
  the machine that ends it with MU left is the fault of its operator. Only an
  `out_of_balance` with less MU than the price of one tick is not scored.
- **A stop with the father alive cannot be told from a stop of the operator.** `StopService`
  and `nodo kill` both call `stop_instance` with the reason `manual_kill`. No MU are lost
  in this case (the leftover goes to the father, and its statement shows the `refund`), but
  the father loses the service, and only the father can say "I did not ask for this stop".
  Ideas for later, not done:
  - The node of the father keeps the stops that the father asked for. A `stop` of a
    delegated child that the father did not ask for is scored locally (for example
    `unrequested_stop`). It is a local judgement, not a portable proof.
  - A proof that others can check: the node of the father writes a row in the chain of the
    father each time the father asks for a stop (the `instance_ref` of the child). A `stop`
    of the child with no such row before it shows that the father did not ask for it.
    The statement does not record what the service does, and a run of a VM cannot be
    reproduced for certain, so the statements alone cannot prove it.
  - When the father and the child run on the same node, the operator controls both, and no
    other node can contradict it.
  - Today nothing records a stop that the father asks for: `StopService` leaves a log line
    and the `stop` row of the child, which has the reason and the leftover but not who
    asked. There is no execution receipt for `StopService` either.
  - `Observe` is not the tool for this. It captures the packets of the tap of a local
    instance, and the node that captures it also signs it, so it proves nothing against
    that node. A stop of the father would show on the tap of the father, not of the child,
    and it would still be a claim of the node of the father. A row for each stop is
    cheaper and exact, and needs no `CAP_NET_RAW` and no stored captures.
  - To give `Observe` to ancestors would show the packets of a descendant, that can run on
    another node with other owners of the data, and it would send a stream of packets
    through the nodes with a budget of hops and a payment for each leg. If it is done, give
    only the counters (CPU, memory, bytes of the network).
  - Hashes of the `Observe` receipts could be written in the rows of the chain, for example
    in the maintenance rows. The node would then be bound to one history of captures, and a
    different one later shows as a contradiction. It does not stop a node that invents all
    the captures from the start, which only another source (the tunnel of the ancestor)
    can show. With a retention of 0, each point is a row for ever, so they must be few.
    This can serve to show that the descendant is active, not to show who asked for a stop.
- **A relayed statement keeps only the last signature.** The signature of the node that
  wrote the rows is not kept, so an ancestor can show a lie only against the first node
  that it asked. Each node answers for the peers that it asks.
- **The orphan pays each probe.** A `DescendantTunnel` costs the descendant
  `TUNNEL_OPEN_MU` on each leg that holds its account. Frequent probes make the life of
  the orphan shorter. Reads are paid by the called instance in the same way.
- **A relay leg with no account is free.** A descendant below a peer has no account on a
  node above that peer, so only the limits of the operator apply to that leg.
- **A broken service looks like a missing one.** `maintenance_while_unreachable` is a local
  judgement that cannot tell a VM that runs a service that does not listen from a VM that
  is not there.
- **Mixed versions.** A peer that does not know `instance_ref` in the statement (field 14)
  cannot give the statement of a descendant. When that peer must answer, it is
  `statement_inconsistent`.

## See also

- [`CONFIG.md`](CONFIG.md#balancers) — every `balancers:` key and its default.
- [`DONATIONS.md`](DONATIONS.md) — why the donation term exists and what bounds it.
- [`PRICING.md`](PRICING.md) — what `cost_mu` is.
- [`ERGO.md`](ERGO.md) — the reputation contract and what burning means.
