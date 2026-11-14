# Off-chain reputation (#652)

A node publishes its opinions about peers on Ergo (`docs/ERGO.md`). Each opinion there
costs a transaction, and the burned ERG is what gives it weight. This proposal adds the
same opinions **off the chain**: the node signs them with its identity key (the
`peer_id`) and other nodes read them peer to peer, at no cost.

An opinion can be a score, or a **report of a fault** of a peer. A fault report can
carry the signed messages of the accused peer that prove the fault. A node that gets
such a report checks the proof itself, and does not need to trust the reporter.

## Audit: what the code does today

- `r`: the event log of this node about each peer (`reputation_events`,
  `interface.compute_reputation`). Faults lower it by 1000
  (`statement_check.penalise_once`, `balance_statement._judge_*`).
- `ô`: opinions on Ergo, weighed by `cred(p) = max(0, cos(v_p, r̂))` and by the burned
  ERG (`onchain_credit.py`, `onchain_indexer.py`, table `onchain_opinions`).
- A fault is recorded in `statement_faults` (peer, account, reason, seq). The table
  keeps no evidence. No raw `BalanceStatement` is kept anywhere: the node checks each
  page and keeps only the mark (`delegated_instances.peer_statement_*`).
- `EstimatedCost` has no signature. The delegating node keeps the quote of the peer as
  plain columns (`delegated_instances.quote_*`). So `maintenance_overcharged` and the
  closing-time faults rest on a quote that the peer did not sign.
- The `start` movement of an instance holds `detail.execution_name` and
  `detail.receipt_digest` (`ledger.START`). The statement signature covers `detail`
  (`ledger.movement_hash`).

Which faults a third party can check from signed data alone, today:

| Reason | Signed data | Third party can check |
|---|---|---|
| `statement_inconsistent`, a broken chain in one page | one statement | yes |
| `statement_inconsistent`, two heads for one seq | two statements | yes |
| `statement_inconsistent`, a refusal or a clock far off | nothing | no |
| `killed_with_mu` | one statement of the descendant | yes |
| `statement_contradicts_receipt` | statement and receipt | yes |
| `maintenance_overcharged` | statement, quote not signed | **after this change** |
| `instance_hibernated_unannounced`, `instance_stopped_unannounced` | statement, quote not signed | **after this change** |
| `statement_overcharged` | needs the start references and reservations of the reporter | no |
| `maintenance_while_unreachable` | needs the tunnel failures of the reporter | no |
| `get_service_wrong_hash` | nothing | no |

## Design

### 1. The quote is in the signed `start` movement

When a node starts an instance, it writes its quote for that instance in
`detail.quote` of the `start` movement. The quote holds the figures of `EstimatedCost`
(`init_maintenance_cost`, `max_maintenance_cost`, `maintenance_seconds_loop`,
`price_bounded`, `price_volatility`, `volatility_period_seconds`, `closes_at`,
`on_close`) and the start shape that the node reserved (`mem_limit`, `disk_space`,
`cpu_period`, `cpu_quota`).

The statement signature covers the movement, so the node signs the quote and binds it
to the account of the instance. No new signature is necessary, and the token stays
secret: a reader names the account by its `instance_ref`.

The delegating node reads the quote from the `start` row of the child, and the launch
time from the date of that row. When the `start` row has no quote (an older peer), the
node uses the `quote_*` columns, as before, and the fault is not provable.

### 2. Stateless fault proofs

`src/reputation_system/fault_proofs.py` checks a `FaultEvidence` with only the public
key of the accused peer. It imports no database. It returns the reason that the
evidence proves, or nothing.

| Reason | Evidence | Check |
|---|---|---|
| `statement_inconsistent` | 1 statement | signature, then `verify_chain` in the page |
| `statement_inconsistent` | 2 statements of one account | both signatures, one seq with two hashes |
| `killed_with_mu` | 1 statement of a descendant | the rule of `balance_statement._judge_stop` |
| `statement_contradicts_receipt` | 1 statement, receipts | `receipt_statement.check` |
| `maintenance_overcharged` | the page with the `start` row, the page with the charges | quote of the `start` row, `maintenance_overcharge` from the first row of the page |
| `instance_hibernated_unannounced`, `instance_stopped_unannounced` | the same pages | quote of the `start` row, `closing_check.judge` |

The reporter runs the same check on the evidence before it attaches it. If the
evidence alone does not prove the fault (for example, the reporter judged with a mark
that is not in the pages), the report goes without evidence. So a node never publishes
evidence that the receiver cannot check.

The node keeps the raw pages that prove a fault in a new table `fault_evidence`, and
the first page of each delegated child (the page with its `start` row).

### 3. The off-chain reputation proof

A `ReputationProof` is the same model as a proof on Ergo, signed by the identity key:

- `publisher`: the `peer_id` of the signer. It is the proof id and the owner.
- `ts`: unix seconds. A node keeps the newest proof of each publisher.
- `opinions`: each one has `type` (R4), `object` (R5), `amount`, `positive` (R8),
  `content` (R9) and an optional `evidence`.

The opinions of a node are:

- one score for each peer: `object` is its `peer_id`, `amount` is `|r|`, `positive` is
  `r >= 0`;
- one fault report for each fault, with `amount` 0, `content` the reason as JSON, and
  the evidence when the fault is provable. The score already counts the fault, so the
  report adds no weight of its own.

The signed text is `"celaut-reputation-proof:" + publisher + "|" + digest`. The digest
is built field by field, as the digest of a statement.

### 4. Transport

A new RPC `GetReputationProofs` gives the proof of the node and the proofs of other
nodes that it holds, at most `reputation.MAX_OFFCHAIN_PROOFS_PER_RESPONSE` per call. A relay sends
each proof as its publisher signed it, as gossip does for a `Peer` (#427). A tick pulls
from one random peer every `reputation.OFFCHAIN_PULL_INTERVAL_SECONDS`.

The node keeps a proof only when its publisher is a peer in its database. Only those
publishers can have a weight (see 5), so other proofs cannot fill the disk. The node
checks the evidence of every proof that it gets, also of an unknown publisher: a valid
proof needs no trust.

### 5. Weight

A fault with valid evidence goes into `r` of the accused peer, with the same penalty
as a fault that this node saw itself, once for each peer, reason and evidence digest.

The scores go into a new term of the balancer:

```
v(p, s) = sign · amount / (amount + REPUTATION_HALF_CREDIT)     the r̂ of p for s
w(p)    = cred(p) · max(0, r̂(p))
ŝ(s)    = Σ_p w(p) · v(p, s) / (Σ_p w(p) + OFFCHAIN_REPUTATION_PRIOR)

score(peer) += OFFCHAIN_REPUTATION_WEIGHT · ŝ
```

`cred(p)` is the cosine between the scores of `p` and our `r̂`, as for `ô`. `r̂(p)` is
our own score of the publisher. A publisher that we did not deal with, or that failed
us, has no weight, so new identities cost nothing and give nothing. The subject's own
opinion of itself does not count.

### 6. Ergo

`submit_to_ledger` publishes each fault report as an opinion box against the peer:
one token, R8 false, and R9 the reason and the digest of the evidence. The evidence
stays off the chain: a reader gets it with `GetReputationProofs`.

## Known limits

- A fault that needs knowledge of the reporter (see the audit) is published without
  evidence. It changes nothing at the receiver: only the score of the reporter counts.
- `cred` can be copied, as for `ô`: a mirror of our scores gets `cred ≈ 1`. Here it also
  needs `r̂(p) > 0`, that is, good work for us.
- A proof is only as fresh as the last pull.
